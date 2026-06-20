from .api_terminal import ROUTE_SPECS_TERMINAL
from .api_terminal_orders import ROUTE_SPECS_TERMINAL_ORDERS

ROUTE_SPECS_TERMINAL_ALL = (
    list(ROUTE_SPECS_TERMINAL)
    + list(ROUTE_SPECS_TERMINAL_ORDERS)
)