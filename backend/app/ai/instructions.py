from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT

# Kept for backward-compatible imports; Realtime uses VOICE_AGENT_SYSTEM_PROMPT.
SYSTEM_INSTRUCTIONS = VOICE_AGENT_SYSTEM_PROMPT

CLARIFICATION_PROMPTS = {
    "location": "Kaun si area ya DHA phase dekh rahe hain?",
    "budget": "Aapka budget kitna hai?",
    "size": "Kitne marla ya size chahiye?",
    "purpose": "Aap rent ke liye dekh rahe hain ya buy ke liye?",
}
