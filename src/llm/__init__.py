"""LLM helpers for narration."""

from src.llm.chain import LLMChain, TEMPLATE_SENTINEL, get_llm_chain, parse_llm_chain_specs

__all__ = [
    "LLMChain",
    "TEMPLATE_SENTINEL",
    "get_llm_chain",
    "parse_llm_chain_specs",
]
