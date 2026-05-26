from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.user_connections: dict[str, set[WebSocket]] = {}
        self.market_connections: dict[str, set[WebSocket]] = {}
        self.websocket_users: dict[WebSocket, str] = {}
        self.websocket_markets: dict[WebSocket, set[str]] = {}

    async def connect_user(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.websocket_users[websocket] = user_id
        self.websocket_markets[websocket] = set()
        self.user_connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, websocket: WebSocket):
        user_id = self.websocket_users.pop(websocket, None)
        if user_id is not None:
            connections = self.user_connections.get(user_id)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    self.user_connections.pop(user_id, None)

        markets = self.websocket_markets.pop(websocket, set())
        for market in markets:
            connections = self.market_connections.get(market)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    self.market_connections.pop(market, None)

    def subscribe_market(self, websocket: WebSocket, market: str):
        self.websocket_markets.setdefault(websocket, set()).add(market)
        self.market_connections.setdefault(market, set()).add(websocket)

    def unsubscribe_market(self, websocket: WebSocket, market: str):
        self.websocket_markets.setdefault(websocket, set()).discard(market)
        connections = self.market_connections.get(market)
        if connections is not None:
            connections.discard(websocket)
            if not connections:
                self.market_connections.pop(market, None)

    async def send_to_user(self, user_id: str, message: dict):
        await self._send_to_connections(
            self.user_connections.get(user_id, set()),
            message
        )

    async def send_to_market(self, market: str, message: dict):
        await self._send_to_connections(
            self.market_connections.get(market, set()),
            message
        )

    async def _send_to_connections(self, connections: set[WebSocket], message: dict):
        disconnected = []
        for websocket in list(connections):
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(websocket)


manager = ConnectionManager()
