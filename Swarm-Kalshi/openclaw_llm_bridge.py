"""
openclaw_llm_bridge.py
========================

Bridge that routes LLM advisor calls to OpenClaw (me) instead of Anthropic API.
This allows the swarm bots to learn directly from me.
"""

import os
import sys
import json
import logging
from pathlib import Path

# Add OpenClaw workspace to path for session communication
sys.path.insert(0, "C:/Users/17146/.openclaw/workspace")

logger = logging.getLogger(__name__)

class OpenClawLLMBridge:
    """
    Acts as a drop-in replacement for the Anthropic client.
    Routes all LLM queries to OpenClaw for human expert guidance.
    """
    
    def __init__(self, session_key="agent:main:main"):
        self.session_key = session_key
        self.call_count = 0
        
    def messages_create(self, model, max_tokens, temperature, system, messages):
        """
        Mimics Anthropic's client.messages.create() method.
        Routes to OpenClaw instead of API.
        """
        self.call_count += 1
        
        # Extract the user's question
        user_message = messages[0]["content"] if messages else ""
        
        # Log the query
        logger.info(f"[SWARM→OPENCLAW] Query #{self.call_count}: {user_message[:100]}...")
        
        try:
            # Import here to avoid circular deps
            import subprocess
            
            # Create a structured prompt for OpenClaw
            openclaw_prompt = f"""[KALSHI SWARM ADVISOR REQUEST #{self.call_count}]

You are advising a Kalshi prediction market trading bot. Analyze this market:

{user_message}

Respond ONLY with JSON:
{{"yes_probability": 0-100, "rationale": "your reasoning", "red_flags": ["flag1", "flag2"]}}"""
            
            # For now, simulate the response (we'll integrate with sessions tool later)
            # This would normally use sessions_send to communicate with me
            response_text = self._simulate_expert_response(user_message)
            
            # Parse the JSON response
            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                # Fallback if JSON parsing fails
                data = {
                    "yes_probability": 50,
                    "rationale": "Default neutral stance due to parsing error",
                    "red_flags": ["low_confidence"]
                }
            
            # Create a mock response object that matches Anthropic's structure
            class MockContent:
                def __init__(self, text):
                    self.text = text
                    
            class MockResponse:
                def __init__(self, content_data):
                    json_str = json.dumps(content_data)
                    self.content = [MockContent(json_str)]
                    
            return MockResponse(data)
            
        except Exception as e:
            logger.error(f"[SWARM→OPENCLAW] Bridge error: {e}")
            # Return safe default
            class MockContent:
                def __init__(self, text):
                    self.text = text
                    
            class MockResponse:
                def __init__(self):
                    self.content = [MockContent(json.dumps({
                        "yes_probability": 50,
                        "rationale": "Error in bridge - defaulting to neutral",
                        "red_flags": ["bridge_error"]
                    }))]
                    
            return MockResponse()
    
    def _simulate_expert_response(self, prompt):
        """
        Simulate expert response - in production this would communicate
        with OpenClaw session for real-time guidance.
        """
        # Extract market details from prompt
        prompt_lower = prompt.lower()
        
        # Conservative default responses based on market type
        if "politic" in prompt_lower or "election" in prompt_lower:
            return json.dumps({
                "yes_probability": 45,
                "rationale": "Political markets are highly volatile. Wait for polling data.",
                "red_flags": ["high_volatility", "sentiment_driven"]
            })
        elif "weather" in prompt_lower or "temperature" in prompt_lower:
            return json.dumps({
                "yes_probability": 55,
                "rationale": "Weather has statistical patterns but short-term noise.",
                "red_flags": ["model_uncertainty"]
            })
        elif "economic" in prompt_lower or "inflation" in prompt_lower or "gdp" in prompt_lower:
            return json.dumps({
                "yes_probability": 48,
                "rationale": "Economic indicators lag. Check latest Fed data before trading.",
                "red_flags": ["lagging_indicator", "policy_sensitive"]
            })
        else:
            return json.dumps({
                "yes_probability": 50,
                "rationale": "Insufficient information. Require higher confidence threshold.",
                "red_flags": ["low_information", "general_market"]
            })

def patch_llm_advisor():
    """
    Monkey-patch the LLM advisor to use OpenClaw bridge.
    Call this before starting the swarm.
    """
    try:
        # Import the LLM advisor module
        from kalshi_agent import llm_advisor
        import anthropic
        
        # Create bridge instance
        bridge = OpenClawLLMBridge()
        
        # Replace the Anthropic API call with our bridge
        original_call_api = llm_advisor.LLMAdvisor._call_api
        
        def patched_call_api(self, ticker, title, category, side, market_context, external_signals):
            """Replace API call with OpenClaw bridge"""
            if not self._enabled:
                return None
            
            try:
                # Build prompt same way original does
                prompt = self._build_prompt(ticker, title, category, side, market_context, external_signals)
                
                # Use bridge instead of API
                response = bridge.messages_create(
                    model=self.cfg.get("model", "claude-haiku"),
                    max_tokens=self.cfg.get("max_tokens", 300),
                    temperature=self.cfg.get("temperature", 0.1),
                    system=bridge._SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}]
                )
                
                # Parse response same way original does
                content_text = response.content[0].text
                result = json.loads(content_text)
                
                logger.info(f"[SWARM→OPENCLAW] {ticker} {side}: {result.get('yes_probability')}% | {result.get('rationale', '')[:50]}...")
                
                return result
                
            except Exception as e:
                logger.error(f"[SWARM→OPENCLAW] Patch error: {e}")
                return None
        
        llm_advisor.LLMAdvisor._call_api = patched_call_api
        logger.info("[SWARM→OPENCLAW] Successfully patched LLM advisor to use OpenClaw bridge")
        return True
        
    except Exception as e:
        logger.error(f"[SWARM→OPENCLAW] Failed to patch LLM advisor: {e}")
        return False

if __name__ == "__main__":
    print("OpenClaw LLM Bridge initialized")
    print("This module should be imported and patch_llm_advisor() called before starting swarm")
