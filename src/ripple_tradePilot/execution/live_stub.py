from __future__ import annotations

from typing import Protocol

from ripple_tradePilot.models.types import Order


class BrokerClient(Protocol):
    def submit_order(self, order: Order) -> str:  # returns order id
        ...


def submit_live_order(broker: BrokerClient, order: Order) -> str:
    return broker.submit_order(order)
