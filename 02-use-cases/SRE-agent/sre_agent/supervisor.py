#!/usr/bin/env python3

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal

from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from .agent_state import AgentState
from .constants import SREConstants
from .output_formatter import create_formatter
from .prompt_loader import prompt_loader
from .memory.client import SREMemoryClient
from .memory.hooks import MemoryHookProvider
from .memory.config import _load_memory_config
from .memory import create_conversation_memory_manager


def _get_user_from_env() -> str:
    """Get user_id from environment variable.
    
    Returns:
        user_id from USER_ID environment variable or default
    """
    user_id = os.getenv("USER_ID")
    if user_id:
        logger.info(f"Using user_id from environment: {user_id}")
        return user_id
    else:
        # Fallback to default user_id
        default_user_id = SREConstants.agents.default_user_id
        logger.warning(f"USER_ID not set in environment, using default: {default_user_id}")
        return default_user_id


def _get_session_from_env(mode: str) -> str:
    """Get session_id from environment variable or generate one.
    
    Args:
        mode: "interactive" or "prompt" for auto-generation prefix
    
    Returns:
        session_id from SESSION_ID environment variable or auto-generated
    """
    session_id = os.getenv("SESSION_ID")
    if session_id:
        logger.info(f"Using session_id from environment: {session_id}")
        return session_id
    else:
        # Auto-generate session_id
        auto_session_id = f"{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        logger.info(f"SESSION_ID not set in environment, auto-generated: {auto_session_id}")
        return auto_session_id


# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

# Enable HTTP and MCP protocol logs for debugging
# Comment out the following lines to suppress these logs if needed
# mcp_loggers = ["streamable_http", "mcp.client.streamable_http", "httpx", "httpcore"]
#
# for logger_name in mcp_loggers:
#     mcp_logger = logging.getLogger(logger_name)
#     mcp_logger.setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _json_serializer(obj):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


class InvestigationPlan(BaseModel):
    """Investigation plan created by supervisor."""

    steps: List[str] = Field(
        description="List of 3-5 investigation steps to be executed"
    )
    
    @field_validator('steps', mode='before')
    @classmethod
    def validate_steps(cls, v):
        """Convert string steps to list if needed."""
        if isinstance(v, str):
            # Split by numbered lines and clean up
            import re
            lines = v.strip().split('\n')
            steps = []
            for line in lines:
                line = line.strip()
                if line:
                    # Remove numbering like "1.", "2.", etc.
                    clean_line = re.sub(r'^\d+\.\s*', '', line)
                    if clean_line:
                        steps.append(clean_line)
            return steps
        return v
    agents_sequence: List[str] = Field(
        description="Sequence of agents to invoke (kubernetes_agent, logs_agent, metrics_agent, runbooks_agent)"
    )
    complexity: Literal["simple", "complex"] = Field(
        description="Whether this plan is simple (auto-execute) or complex (needs approval)"
    )
    auto_execute: bool = Field(
        description="Whether to execute automatically or ask for user approval"
    )
    reasoning: str = Field(
        description="Brief explanation of the investigation approach"
    )


class RouteDecision(BaseModel):
    """Decision made by supervisor for routing."""

    next: Literal["kubernetes_agent", "logs_agent", "metrics_agent", "runbooks_agent", "FINISH"] = Field(
        description="The next agent to route to, or FINISH if done"
    )
    reasoning: str = Field(
        description="Brief explanation of why this routing decision was made"
    )


def _read_supervisor_prompt() -> str:
    """Read supervisor system prompt from file."""
    try:
        prompt_path = (
            Path(__file__).parent
            / "config"
            / "prompts"
            / "supervisor_multi_agent_prompt.txt"
        )
        if prompt_path.exists():
            return prompt_path.read_text().strip()
    except Exception as e:
        logger.warning(f"Could not read supervisor prompt file: {e}")

    # Default prompt if file not found
    return """You are the Supervisor Agent orchestrating a team of specialized SRE agents.

Your team consists of:
1. Kubernetes Infrastructure Agent - Handles K8s cluster operations, pod status, deployments, and resource monitoring
2. Application Logs Agent - Analyzes logs, searches for patterns, and identifies errors
3. Performance Metrics Agent - Monitors application performance, resource usage, and availability
4. Operational Runbooks Agent - Provides troubleshooting guides and operational procedures

Your responsibilities:
- Analyze incoming queries and determine which agent(s) should handle them
- Route queries to the most appropriate agent based on the query content
- Determine if multiple agents need to collaborate
- Aggregate responses when multiple agents are involved
- Provide clear, actionable responses to users

Routing guidelines:
- For Kubernetes/infrastructure issues → kubernetes_agent
- For log analysis or error investigation → logs_agent  
- For performance/metrics questions → metrics_agent
- For procedures/troubleshooting guides → runbooks_agent
- For complex issues spanning multiple domains → multiple agents

Always consider if a query requires multiple perspectives. For example:
- "Why is my service down?" might need kubernetes_agent (pod status) + logs_agent (errors) + metrics_agent (performance)
- "Debug high latency" might need metrics_agent (performance data) + logs_agent (error patterns)"""


class SupervisorAgent:
    """Supervisor agent that orchestrates other agents with memory capabilities."""

    def __init__(self, llm_provider: str = "anthropic", force_delete_memory: bool = False, **llm_kwargs):
        self.llm_provider = llm_provider
        self.llm = self._create_llm(**llm_kwargs)
        self.system_prompt = _read_supervisor_prompt()
        self.formatter = create_formatter(llm_provider=llm_provider)
        
        # Initialize memory system
        self.memory_config = _load_memory_config()
        if self.memory_config.enabled:
            self.memory_client = SREMemoryClient(
                memory_name=self.memory_config.memory_name,
                region=self.memory_config.region,
                force_delete=force_delete_memory
            )
            self.memory_hooks = MemoryHookProvider(self.memory_client)
            self.conversation_manager = create_conversation_memory_manager(self.memory_client)
            logger.info("Memory system initialized for supervisor agent")
        else:
            self.memory_client = None
            self.memory_hooks = None
            self.conversation_manager = None
            logger.info("Memory system disabled")

    def _create_llm(self, **kwargs):
        """Create LLM instance based on provider."""
        config = SREConstants.get_model_config(self.llm_provider, **kwargs)

        if self.llm_provider == "anthropic":
            return ChatAnthropic(
                model=config["model_id"],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
            )
        elif self.llm_provider == "bedrock":
            return ChatBedrock(
                model_id=config["model_id"],
                region_name=config["region_name"],
                model_kwargs={
                    "temperature": config["temperature"],
                    "max_tokens": config["max_tokens"],
                },
            )
        else:
            raise ValueError(f"Unsupported provider: {self.llm_provider}")

    async def create_investigation_plan(self, state: AgentState) -> InvestigationPlan:
        """Create an investigation plan for the user's query with memory context."""
        current_query = state.get("current_query", "No query provided")
        user_id = state.get("user_id", SREConstants.agents.default_user_id)
        incident_id = state.get("incident_id")
        # Use user_id as actor_id for investigation memory retrieval (consistent with storage)
        actor_id = state.get("user_id", state.get("actor_id", SREConstants.agents.default_actor_id))
        session_id = state.get("session_id")
        
        # Retrieve memory context if memory system is enabled
        memory_context_text = ""
        if self.memory_client:
            try:
                logger.debug(f"Retrieving memory context for user_id={user_id}, query='{current_query}'")
                
                # Get memory context from hooks
                if not session_id:
                    raise ValueError("session_id is required for memory retrieval but not found in state")
                    
                memory_context = self.memory_hooks.on_investigation_start(
                    query=current_query,
                    user_id=user_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    incident_id=incident_id
                )
                
                # Store memory context in state
                state["memory_context"] = memory_context
                
                # Log user preferences for debugging (they're stored in memory_context)
                user_prefs = memory_context.get("user_preferences", [])
                logger.info(f"DEBUG: Stored {len(user_prefs)} user preferences in memory_context during planning")
                logger.info(f"DEBUG: User preferences being stored in memory_context: {user_prefs}")
                
                # Format memory context for prompt
                pref_count = len(memory_context.get("user_preferences", []))
                infrastructure_by_agent = memory_context.get("infrastructure_by_agent", {})
                total_knowledge = sum(len(memories) for memories in infrastructure_by_agent.values())
                investigation_count = len(memory_context.get("past_investigations", []))
                
                if memory_context.get("user_preferences"):
                    memory_context_text += f"\nRelevant User Preferences:\n{json.dumps(memory_context['user_preferences'], indent=2, default=_json_serializer)}\n"
                
                if infrastructure_by_agent:
                    memory_context_text += f"\nRelevant Infrastructure Knowledge (organized by agent):\n"
                    for agent_id, agent_memories in infrastructure_by_agent.items():
                        memory_context_text += f"\n  From {agent_id} ({len(agent_memories)} items):\n"
                        memory_context_text += f"{json.dumps(agent_memories, indent=4, default=_json_serializer)}\n"
                
                if memory_context.get("past_investigations"):
                    memory_context_text += f"\nSimilar Past Investigations:\n{json.dumps(memory_context['past_investigations'], indent=2, default=_json_serializer)}\n"
                
                logger.info(f"Retrieved memory context for planning: {pref_count} preferences, {total_knowledge} knowledge items from {len(infrastructure_by_agent)} agents, {investigation_count} past investigations")
                
                if pref_count + total_knowledge + investigation_count == 0:
                    logger.info("No relevant memories found - this may be the first interaction or a new topic")
                
            except Exception as e:
                logger.error(f"Failed to retrieve memory context: {e}", exc_info=True)
                memory_context_text = ""

        planning_prompt = f"""{self.system_prompt}

User's query: {current_query}
{memory_context_text}
Create a simple, focused investigation plan with 2-3 steps maximum. Consider:
- Start with the most relevant single agent
- Add one follow-up agent only if clearly needed
- Keep it simple - most queries need only 1-2 agents
- Mark as simple unless it involves production changes or multiple domains
- Take into account any user preferences and past investigation patterns shown above

Return a structured plan."""

        structured_llm = self.llm.with_structured_output(InvestigationPlan)

        plan = await structured_llm.ainvoke(
            [
                SystemMessage(content=planning_prompt),
                HumanMessage(content=current_query),
            ]
        )

        logger.info(
            f"Created investigation plan: {len(plan.steps)} steps, complexity: {plan.complexity}"
        )
        
        # Store conversation in memory
        if self.conversation_manager and user_id and session_id:
            try:
                # Get supervisor display name with fallback
                supervisor_name = getattr(SREConstants.agents, 'supervisor', None)
                if supervisor_name:
                    supervisor_display_name = supervisor_name.display_name
                else:
                    supervisor_display_name = "Supervisor Agent"
                    
                messages_to_store = [
                    (current_query, "USER"),
                    (f"[Agent: {supervisor_display_name}]\nInvestigation Plan:\n{self._format_plan_markdown(plan)}", "ASSISTANT")
                ]
                
                success = self.conversation_manager.store_conversation_batch(
                    messages=messages_to_store,
                    user_id=user_id,
                    session_id=session_id,
                    agent_name=supervisor_display_name
                )
                
                if success:
                    logger.info("Supervisor: Successfully stored planning conversation")
                else:
                    logger.warning("Supervisor: Failed to store planning conversation")
                    
            except Exception as e:
                logger.error(f"Supervisor: Error storing planning conversation: {e}", exc_info=True)
        
        return plan

    def _format_plan_markdown(self, plan: InvestigationPlan) -> str:
        """Format investigation plan as properly formatted markdown."""
        plan_text = "## 🔍 Investigation Plan\n\n"

        # Add steps with proper numbering and formatting
        for i, step in enumerate(plan.steps, 1):
            plan_text += f"**{i}.** {step}\n\n"

        # Add metadata
        plan_text += f"**📊 Complexity:** {plan.complexity.title()}\n"
        plan_text += f"**🤖 Auto-execute:** {'Yes' if plan.auto_execute else 'No'}\n"
        if plan.reasoning:
            plan_text += f"**💭 Reasoning:** {plan.reasoning}\n"

        # Add agents involved
        if plan.agents_sequence:
            agents_list = ", ".join(
                [agent.replace("_", " ").title() for agent in plan.agents_sequence]
            )
            plan_text += f"**👥 Agents involved:** {agents_list}\n"

        return plan_text

    async def route(self, state: AgentState) -> Dict[str, Any]:
        """Determine which agent should handle the query next."""
        agents_invoked = state.get("agents_invoked", [])

        # Check if we have an existing plan
        existing_plan = state.get("metadata", {}).get("investigation_plan")

        if not existing_plan:
            # First time - create investigation plan
            plan = await self.create_investigation_plan(state)

            # Check if we should auto-approve the plan (defaults to False if not set)
            auto_approve = state.get("auto_approve_plan", False)

            if not plan.auto_execute and not auto_approve:
                # Complex plan - present to user for approval
                plan_text = self._format_plan_markdown(plan)
                return {
                    "next": "FINISH",
                    "metadata": {
                        **state.get("metadata", {}),
                        "investigation_plan": plan.model_dump(),
                        "routing_reasoning": f"Created investigation plan. Complexity: {plan.complexity}",
                        "plan_pending_approval": True,
                        "plan_text": plan_text,
                    },
                    # Preserve memory context in state
                    "memory_context": state.get("memory_context", {}),
                }
            else:
                # Simple plan - start execution
                next_agent = (
                    plan.agents_sequence[0] if plan.agents_sequence else "FINISH"
                )
                plan_text = self._format_plan_markdown(plan)
                return {
                    "next": next_agent,
                    "metadata": {
                        **state.get("metadata", {}),
                        "investigation_plan": plan.model_dump(),
                        "routing_reasoning": f"Executing plan step 1: {plan.steps[0] if plan.steps else 'Start'}",
                        "plan_step": 0,
                        "plan_text": plan_text,
                        "show_plan": True,
                    },
                    # Preserve memory context in state
                    "memory_context": state.get("memory_context", {}),
                }
        else:
            # Continue executing existing plan
            plan = InvestigationPlan(**existing_plan)
            current_step = state.get("metadata", {}).get("plan_step", 0)

            # Check if plan is complete
            if current_step >= len(plan.agents_sequence) or not agents_invoked:
                next_step = current_step
            else:
                next_step = current_step + 1

            if next_step >= len(plan.agents_sequence):
                # Plan complete
                return {
                    "next": "FINISH",
                    "metadata": {
                        **state.get("metadata", {}),
                        "routing_reasoning": "Investigation plan completed. Presenting results.",
                        "plan_step": next_step,
                    },
                    # Preserve memory context in state
                    "memory_context": state.get("memory_context", {}),
                }
            else:
                # Continue with next agent in plan
                next_agent = plan.agents_sequence[next_step]
                step_description = (
                    plan.steps[next_step]
                    if next_step < len(plan.steps)
                    else f"Execute {next_agent}"
                )

                return {
                    "next": next_agent,
                    "metadata": {
                        **state.get("metadata", {}),
                        "routing_reasoning": f"Executing plan step {next_step + 1}: {step_description}",
                        "plan_step": next_step,
                    },
                    # Preserve memory context in state
                    "memory_context": state.get("memory_context", {}),
                }

    async def aggregate_responses(self, state: AgentState) -> Dict[str, Any]:
        """Aggregate responses from multiple agents into a final response."""
        agent_results = state.get("agent_results", {})
        metadata = state.get("metadata", {})

        # Check if this is a plan approval request
        if metadata.get("plan_pending_approval"):
            plan = metadata.get("investigation_plan", {})
            query = state.get("current_query", "Investigation") or "Investigation"

            # Use enhanced formatting for plan approval
            try:
                approval_response = self.formatter.format_plan_approval(plan, query)
            except Exception as e:
                logger.warning(
                    f"Failed to use enhanced formatting: {e}, falling back to plain text"
                )
                plan_text = metadata.get("plan_text", "")
                approval_response = f"""## Investigation Plan

I've analyzed your query and created the following investigation plan:

{plan_text}

**Complexity:** {plan.get('complexity', 'unknown').title()}
**Reasoning:** {plan.get('reasoning', 'Standard investigation approach')}

This plan will help systematically investigate your issue. Would you like me to proceed with this plan, or would you prefer to modify it?

You can:
- Type "proceed" or "yes" to execute the plan
- Type "modify" to suggest changes
- Ask specific questions about any step"""

            return {"final_response": approval_response, "next": "FINISH"}

        if not agent_results:
            return {"final_response": "No agent responses to aggregate."}

        # Use enhanced formatting for investigation results
        query = state.get("current_query", "Investigation") or "Investigation"
        plan = metadata.get("investigation_plan")

        # Get user preferences from memory_context (not directly from state)
        user_preferences = []
        if "memory_context" in state:
            memory_ctx = state["memory_context"]
            user_preferences = memory_ctx.get('user_preferences', [])
            logger.info(f"DEBUG: Memory context found with {len(user_preferences)} user preferences")
        else:
            logger.info("DEBUG: No memory_context found in state")
        
        logger.info(f"Retrieved user preferences from memory_context for aggregation: {len(user_preferences)} items")
        logger.info(f"DEBUG: Full state keys available: {list(state.keys())}")
        
        try:
            # Try enhanced formatting first
            final_response = self.formatter.format_investigation_response(
                query=query, agent_results=agent_results, metadata=metadata, plan=plan, user_preferences=user_preferences
            )
        except Exception as e:
            logger.warning(
                f"Failed to use enhanced formatting: {e}, falling back to LLM aggregation"
            )

            # Fallback to LLM-based aggregation
            try:
                # Get system message from prompt loader
                system_prompt = prompt_loader.load_prompt(
                    "supervisor_aggregation_system"
                )

                # Determine if this is plan-based or standard aggregation
                is_plan_based = plan is not None

                # Prepare template variables
                query = (
                    state.get("current_query", "No query provided")
                    or "No query provided"
                )
                agent_results_json = json.dumps(agent_results, indent=2, default=_json_serializer)
                auto_approve_plan = state.get("auto_approve_plan", False) or False
                
                # Use the user_preferences we already retrieved
                user_preferences_json = json.dumps(user_preferences, indent=2, default=_json_serializer) if user_preferences else ""

                if is_plan_based:
                    current_step = metadata.get("plan_step", 0)
                    total_steps = len(plan.get("steps", []))
                    plan_json = json.dumps(plan.get("steps", []), indent=2, default=_json_serializer)

                    aggregation_prompt = (
                        prompt_loader.get_supervisor_aggregation_prompt(
                            is_plan_based=True,
                            query=query,
                            agent_results=agent_results_json,
                            auto_approve_plan=auto_approve_plan,
                            current_step=current_step + 1,
                            total_steps=total_steps,
                            plan=plan_json,
                            user_preferences=user_preferences_json,
                        )
                    )
                else:
                    aggregation_prompt = (
                        prompt_loader.get_supervisor_aggregation_prompt(
                            is_plan_based=False,
                            query=query,
                            agent_results=agent_results_json,
                            auto_approve_plan=auto_approve_plan,
                            user_preferences=user_preferences_json,
                        )
                    )

            except Exception as e:
                logger.error(f"Error loading aggregation prompts: {e}")
                # Fallback to simple prompt
                system_prompt = "You are an expert at presenting technical investigation results clearly and professionally."
                aggregation_prompt = (
                    f"Summarize these findings: {json.dumps(agent_results, indent=2, default=_json_serializer)}"
                )

            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=aggregation_prompt),
                ]
            )

            final_response = response.content

        # Store final response conversation in memory
        user_id = state.get("user_id")
        session_id = state.get("session_id")
        if self.conversation_manager and user_id and session_id and not metadata.get("plan_pending_approval"):
            try:
                # Store the final aggregated response
                # Get supervisor display name with fallback
                supervisor_name = getattr(SREConstants.agents, 'supervisor', None)
                if supervisor_name:
                    supervisor_display_name = supervisor_name.display_name
                else:
                    supervisor_display_name = "Supervisor Agent"
                    
                messages_to_store = [
                    (f"[Agent: {supervisor_display_name}]\n{final_response}", "ASSISTANT")
                ]
                
                success = self.conversation_manager.store_conversation_batch(
                    messages=messages_to_store,
                    user_id=user_id,
                    session_id=session_id,
                    agent_name=supervisor_display_name
                )
                
                if success:
                    logger.info("Supervisor: Successfully stored final response conversation")
                else:
                    logger.warning("Supervisor: Failed to store final response conversation")
                    
            except Exception as e:
                logger.error(f"Supervisor: Error storing final response conversation: {e}", exc_info=True)

        # Save investigation summary to memory if enabled
        if self.memory_client and not metadata.get("plan_pending_approval"):
            try:
                incident_id = state.get("incident_id", "auto-generated")
                agents_used = state.get("agents_invoked", [])
                logger.debug(f"Saving investigation summary for incident_id={incident_id}, agents_used={agents_used}")
                
                # Use user_id as actor_id for investigation summaries (consistent with conversation memory)
                actor_id = state.get("user_id", state.get("actor_id", SREConstants.agents.default_actor_id))
                self.memory_hooks.on_investigation_complete(
                    state=state,
                    final_response=final_response,
                    actor_id=actor_id
                )
                logger.info(f"Saved investigation summary to memory for incident {incident_id}")
            except Exception as e:
                logger.error(f"Failed to save investigation summary: {e}", exc_info=True)

        return {"final_response": final_response, "next": "FINISH"}
