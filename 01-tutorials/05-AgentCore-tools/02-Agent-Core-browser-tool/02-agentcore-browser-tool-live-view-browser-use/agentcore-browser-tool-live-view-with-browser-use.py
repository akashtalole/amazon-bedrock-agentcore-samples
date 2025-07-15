from browser_use import Agent
from browser_use.browser.session import BrowserSession
from bedrock_agentcore.tools.browser_client import BrowserClient
from browser_use.browser import BrowserProfile
from langchain_aws import ChatBedrockConverse
from rich.console import Console
from rich.panel import Panel
from interactive_tools.browser_viewer import BrowserViewerServer
from contextlib import suppress
import time
import asyncio

console = Console()


async def run_browser_task(
    browser_session: BrowserSession, bedrock_chat: ChatBedrockConverse, task: str
) -> None:
    """
    Run a browser automation task using browser_use

    Args:
        browser_session: Existing browser session to reuse
        bedrock_chat: Bedrock chat model instance
        task: Natural language task for the agent
    """
    try:
        # Show task execution
        console.print(f"\n[bold blue]🤖 Executing task:[/bold blue] {task}")

        # Create and run the agent
        agent = Agent(task=task, llm=bedrock_chat, browser_session=browser_session)

        # Run with progress indicator
        with console.status(
            "[bold green]Running browser automation...[/bold green]", spinner="dots"
        ):
            await agent.run()

        console.print("[bold green]✅ Task completed successfully![/bold green]")

    except Exception as e:
        console.print(f"[bold red]❌ Error during task execution:[/bold red] {str(e)}")
        import traceback

        if console.is_terminal:
            traceback.print_exc()


async def main():
    """
    Main function that demonstrates live browser viewing with Agent automation.

    Workflow:
    1. Creates Amazon Bedrock AgentCore browser client in us-west-2 region
    2. Waits for browser initialization (10-second required delay)
    3. Starts DCV-based live viewer server on port 8000 with browser control
    4. Configures multiple display size options (720p to 1440p)
    5. Establishes browser session for AI agent automation via CDP WebSocket
    6. Executes AI-driven tasks using Claude 3.5 Sonnet model
    7. Properly closes all sessions and stops browser client

    Features:
    - Real-time browser viewing through web interface
    - Manual take/release control functionality
    - AI automation with browser-use library
    - Configurable display layouts and sizes
    """
    console.print(
        Panel(
            "[bold cyan]Genesis Browser Live Viewer[/bold cyan]\n\n"
            "This demonstrates:\n"
            "• Live browser viewing with DCV\n"
            "• Configurable display sizes (not limited to 900×800)\n"
            "• Proper display layout callbacks\n\n"
            "[yellow]Note: Requires Amazon DCV SDK files[/yellow]",
            title="Browser Live Viewer",
            border_style="blue",
        )
    )

    try:
        # Step 1: Create browser session
        client = BrowserClient("us-west-2")
        client.start()
        try:
            console.print(
                "\n[cyan]Step 2: Waiting for browser initialization...[/cyan]"
            )
            console.print(
                "[dim]This 10-second wait is required (will be removed after 6/20)[/dim]"
            )
            for i in range(10, 0, -1):
                print(f"\r   {i} seconds remaining...", end="", flush=True)
                time.sleep(1)
            print("\r   ✅ Browser ready!                    ")
            ws_url, headers = client.generate_ws_headers()

            # Step 2: Start viewer server
            console.print("\n[cyan]Step 3: Starting viewer server...[/cyan]")
            viewer = BrowserViewerServer(client, port=8000)
            viewer_url = viewer.start(open_browser=True)

            # Step 3: Show features
            console.print("\n[bold green]Viewer Features:[/bold green]")
            console.print(
                "• Default display: 1600×900 (configured via displayLayout callback)"
            )
            console.print("• Size options: 720p, 900p, 1080p, 1440p")
            console.print("• Real-time display updates")
            console.print("• Take/Release control functionality")

            console.print("\n[yellow]Press Ctrl+C to stop[/yellow]")

            # Step 4: Use browser-use to interact with browser
            # Create persistent browser session and model
            browser_session = None
            bedrock_chat = None

            try:
                # Create browser profile with headers
                browser_profile = BrowserProfile(
                    headers=headers,
                    timeout=1500000,  # 150 seconds timeout
                )

                # Create a browser session with CDP URL and keep_alive=True for persistence
                browser_session = BrowserSession(
                    cdp_url=ws_url,
                    browser_profile=browser_profile,
                    keep_alive=True,  # Keep browser alive between tasks
                )

                # Initialize the browser session
                console.print("[cyan]🔄 Initializing browser session...[/cyan]")
                await browser_session.start()

                # Create ChatBedrockConverse once
                bedrock_chat = ChatBedrockConverse(
                    model_id="anthropic.claude-3-5-sonnet-20240620-v1:0",
                    region_name="us-west-2",
                )

                console.print(
                    "[green]✅ Browser session initialized and ready for tasks[/green]\n"
                )

                task = "Search for a coffee maker on amazon.com and extract details of the first one"

                await run_browser_task(browser_session, bedrock_chat, task)

            finally:
                # Close the browser session
                if browser_session:
                    console.print("\n[yellow]🔌 Closing browser session...[/yellow]")
                    with suppress(Exception):
                        await browser_session.close()
                    console.print("[green]✅ Browser session closed[/green]")

        finally:
            client.stop()

    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
