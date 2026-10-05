"""Offline checks of gate wiring; these do not certify any live provider."""

import pytest

from app.tools import catalog, cart, orders
from test_regression import has_money, provider_model


@pytest.mark.parametrize("selected_provider", ["gemini", "groq", "openai"])
def test_provider_adapter_binds_all_gate_tools(selected_provider, monkeypatch):
    for name in ("GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "test-only-placeholder")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    tools = catalog.catalog_tools + cart.cart_tools + [orders.get_order_status, orders.get_payment_link]
    model = provider_model(selected_provider, tools)
    assert len(model.kwargs["tools"]) == len(tools)
    assert model.bound.max_retries == 0
    assert not hasattr(model, "fallbacks")


def test_unknown_provider_fails():
    with pytest.raises(ValueError, match="Unsupported provider"):
        provider_model("unknown", catalog.catalog_tools)


def test_gate_money_assertion_requires_exact_amount():
    for reply in ("Rs. 8,990", "Rs. 8,990.00", "Price: Rs. 8,990."):
        has_money(reply, "8,990")
    for reply in ("Rs. 8,990.50", "Rs. 8,9900", "Rs. 8,990,000"):
        with pytest.raises(AssertionError):
            has_money(reply, "8,990")
