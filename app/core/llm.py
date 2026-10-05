"""Tool-bound Gemini primary with a Groq fallback; no agent execution loop."""

import os
from collections.abc import Sequence

from dotenv import load_dotenv
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq


def get_llm(tools: Sequence[BaseTool]) -> Runnable[LanguageModelInput, BaseMessage]:
    """Build Gemini -> Groq with the same tools bound to both providers.

    Requires GOOGLE_API_KEY and GROQ_API_KEY. Provider errors during invocation
    trigger LangChain's fallback; initialization/configuration errors fail early.
    """
    load_dotenv()
    missing = [name for name in ("GOOGLE_API_KEY", "GROQ_API_KEY") if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("Missing required environment variables: " + ", ".join(missing))

    tool_list = list(tools)
    # Disable SDK retries so a 429 reaches the fallback promptly.
    gemini_model = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite"),
        api_key=os.environ["GOOGLE_API_KEY"],
        vertexai=False,
        temperature=0,
        max_retries=0,
        timeout=30,
    ).bind_tools(tool_list)
    groq_model = ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=os.environ["GROQ_API_KEY"],
        temperature=0,
        max_retries=0,
        timeout=30,
    ).bind_tools(tool_list)

    # FUTURE ONLY: insert OpenAI ahead of Groq in the fallback order.
    # Phase 9 supplies langchain-openai and Compose variables for the test gate.
    # Set OPENAI_API_KEY and OPENAI_MODEL and pass that gate before uncommenting:
    # from langchain_openai import ChatOpenAI
    # openai_model = ChatOpenAI(
    #     model=os.environ["OPENAI_MODEL"],
    #     api_key=os.environ["OPENAI_API_KEY"],
    #     max_retries=0,
    #     timeout=30,
    # ).bind_tools(tool_list)
    # return gemini_model.with_fallbacks([openai_model, groq_model])
    # Order above: Gemini -> OpenAI -> Groq. Runtime OpenAI remains disabled.

    return gemini_model.with_fallbacks([groq_model])
