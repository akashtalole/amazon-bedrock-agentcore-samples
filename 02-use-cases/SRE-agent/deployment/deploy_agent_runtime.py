#!/usr/bin/env python3

import argparse
import boto3
import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv
from botocore.exceptions import ClientError

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)


def _write_agent_arn_to_file(
    agent_arn: str,
    output_dir: str = None
) -> None:
    """Write agent ARN to .agent_arn file."""
    if output_dir is None:
        output_dir = Path(__file__).parent
    else:
        output_dir = Path(output_dir)
    
    arn_file = output_dir / ".agent_arn"
    
    try:
        with open(arn_file, 'w') as f:
            f.write(agent_arn)
        logging.info(f"💾 Agent Runtime ARN saved to {arn_file}")
    except Exception as e:
        logging.error(f"Failed to write agent ARN to file: {e}")


def _get_agent_runtime_id_by_name(
    client: boto3.client,
    runtime_name: str
) -> str:
    """Get agent runtime ID by name."""
    try:
        response = client.list_agent_runtimes()
        agent_runtimes = response.get('agentRuntimes', [])
        
        for runtime in agent_runtimes:
            if runtime['agentRuntimeName'] == runtime_name:
                return runtime['agentRuntimeId']
        
        return None
        
    except ClientError as e:
        logging.error(f"Failed to get agent runtime ID: {e}")
        return None


def _delete_agent_runtime(
    client: boto3.client,
    runtime_id: str
) -> bool:
    """Delete an agent runtime by ID."""
    try:
        logging.info(f"Deleting agent runtime with ID: {runtime_id}")
        client.delete_agent_runtime(agentRuntimeId=runtime_id)
        logging.info("Agent runtime deleted successfully")
        return True
        
    except ClientError as e:
        logging.error(f"Failed to delete agent runtime: {e}")
        return False


def _list_existing_agent_runtimes(
    client: boto3.client
) -> None:
    """List all existing agent runtimes."""
    try:
        response = client.list_agent_runtimes()
        agent_runtimes = response.get('agentRuntimes', [])
        
        if not agent_runtimes:
            logging.info("No existing agent runtimes found.")
            return
            
        logging.info("Existing agent runtimes:")
        for runtime in agent_runtimes:
            logging.info(json.dumps(runtime, indent=2, default=str))
            
    except ClientError as e:
        logging.error(f"Failed to list agent runtimes: {e}")


def _create_agent_runtime(
    client: boto3.client,
    runtime_name: str,
    container_uri: str,
    role_arn: str,
    anthropic_api_key: str,
    gateway_access_token: str,
    force_recreate: bool = False
) -> None:
    """Create an agent runtime with error handling for conflicts."""
    try:
        response = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            agentRuntimeArtifact={
                'containerConfiguration': {
                    'containerUri': container_uri
                }
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            environmentVariables={
                'ANTHROPIC_API_KEY': anthropic_api_key,
                'GATEWAY_ACCESS_TOKEN': gateway_access_token
            }
        )
        
        logging.info(f"Agent Runtime created successfully!")
        logging.info(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
        logging.info(f"Status: {response['status']}")
        _write_agent_arn_to_file(response['agentRuntimeArn'])
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == 'ConflictException':
            logging.error(f"Agent runtime '{runtime_name}' already exists.")
            logging.info("Listing existing agent runtimes:")
            _list_existing_agent_runtimes(client)
            
            if force_recreate:
                # Get the runtime ID and attempt to delete
                runtime_id = _get_agent_runtime_id_by_name(client, runtime_name)
                if runtime_id:
                    if _delete_agent_runtime(client, runtime_id):
                        logging.info("Attempting to recreate agent runtime...")
                        # Retry creation after deletion
                        response = client.create_agent_runtime(
                            agentRuntimeName=runtime_name,
                            agentRuntimeArtifact={
                                'containerConfiguration': {
                                    'containerUri': container_uri
                                }
                            },
                            networkConfiguration={"networkMode": "PUBLIC"},
                            roleArn=role_arn,
                            environmentVariables={
                                'ANTHROPIC_API_KEY': anthropic_api_key,
                                'GATEWAY_ACCESS_TOKEN': gateway_access_token
                            }
                        )
                        
                        logging.info(f"Agent Runtime recreated successfully!")
                        logging.info(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
                        logging.info(f"Status: {response['status']}")
                        _write_agent_arn_to_file(response['agentRuntimeArn'])
                    else:
                        logging.error("Failed to delete existing runtime")
                else:
                    logging.error(f"Could not find runtime ID for '{runtime_name}'")
            else:
                logging.info("Please retry with a new agent name using the --runtime-name parameter, or use --force-recreate to delete and recreate.")
        else:
            logging.error(f"Failed to create agent runtime: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(description="Deploy SRE Agent to AgentCore Runtime")
    parser.add_argument(
        "--runtime-name",
        default="sre-agent",
        help="Name for the agent runtime (default: sre-agent)"
    )
    parser.add_argument(
        "--container-uri",
        required=True,
        help="Container URI (e.g., account-id.dkr.ecr.us-west-2.amazonaws.com/my-agent:latest)"
    )
    parser.add_argument(
        "--role-arn",
        required=True,
        help="IAM role ARN for the agent runtime"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="Delete existing runtime if it exists and recreate it"
    )
    
    args = parser.parse_args()
    
    # Load environment variables from .env file
    script_dir = Path(__file__).parent
    env_file = script_dir / ".env"
    
    if env_file.exists():
        load_dotenv(env_file)
        logging.info(f"Loaded environment variables from {env_file}")
    else:
        logging.error(f".env file not found at {env_file}")
        raise FileNotFoundError(f"Please create a .env file at {env_file} with ANTHROPIC_API_KEY and GATEWAY_ACCESS_TOKEN")
    
    # Get environment variables
    anthropic_api_key = os.getenv('ANTHROPIC_API_KEY')
    gateway_access_token = os.getenv('GATEWAY_ACCESS_TOKEN')
    
    if not anthropic_api_key:
        logging.error("ANTHROPIC_API_KEY not found in .env")
        raise ValueError("ANTHROPIC_API_KEY must be set in .env")
    
    if not gateway_access_token:
        logging.error("GATEWAY_ACCESS_TOKEN not found in .env")
        raise ValueError("GATEWAY_ACCESS_TOKEN must be set in .env")
    
    client = boto3.client('bedrock-agentcore-control', region_name=args.region)
    
    _create_agent_runtime(
        client=client,
        runtime_name=args.runtime_name,
        container_uri=args.container_uri,
        role_arn=args.role_arn,
        anthropic_api_key=anthropic_api_key,
        gateway_access_token=gateway_access_token,
        force_recreate=args.force_recreate
    )


if __name__ == "__main__":
    main()