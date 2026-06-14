import asyncio
from .db import get_cached_flow, save_flow
from .utils import is_final_submit
from .agent import run_react_agent
from .mcp_client import PlaywrightMCPClient

async def process_form(url: str):
    domain = url.split("//")[-1].split("/")[0]
    flow_sequence = get_cached_flow(domain)
    
    # 1. Discovery Phase (if no cache exists)
    if not flow_sequence:
        print(f"[Executor] No cached flow for {domain}. Handing to Agent...")
        flow_sequence = await run_react_agent(url)
        if not flow_sequence:
            return {"status": "failed_generation"}
        save_flow(domain, flow_sequence)

    # 2. Setup MCP Client
    mcp_client = PlaywrightMCPClient()
    await mcp_client.connect()
    
    try:
        # 3. Execution Phase
        for i, step in enumerate(flow_sequence):
            tool = step.get('tool')
            params = step.get('parameters', {})
            
            # Dry-Run / Intercept Check
            if is_final_submit(tool, params):
                print(f"[DRY RUN] Intercepted final submit step: {params}")
                # Take the long screenshot before exiting
                await mcp_client.execute_tool("playwright_screenshot", {"name": f"log_{domain}_final.png"})
                return {"status": "dry_run_complete"}

            try:
                # Execute standard Playwright MCP tool
                await mcp_client.execute_tool(tool, params)
                
            except Exception as e:
                print(f"[Executor] Flow failed at step {i + 1}. Triggering self-healing...")
                
                # Close current broken session
                await mcp_client.close()
                
                # Trigger self-healing
                error_context = {
                    "previousFlow": flow_sequence,
                    "failedStepIndex": i,
                    "errorDetails": str(e)
                }
                new_flow = await run_react_agent(url, error_context)
                save_flow(domain, new_flow)
                
                # Return state so caller can restart the process
                return {"status": "healed_needs_restart"}
                
    finally:
        # Ensure subprocess is always cleaned up
        await mcp_client.close()

    return {"status": "flow_complete"}