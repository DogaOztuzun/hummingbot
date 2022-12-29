import asyncio
import logging
from collections import deque
from typing import Any, Dict, Optional

import aiohttp
import numpy as np
import pandas as pd

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.network_base import NetworkBase
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger


class BinanceCandlesFeed(NetworkBase):
    _bcf_logger: Optional[HummingbotLogger] = None
    _binance_candles_shared_instance: "BinanceCandlesFeed" = None
    base_url = "https://api.binance.com"
    health_check_endpoint = "/api/v3/ping"
    candles_endpoint = "/api/v3/klines"
    wss_url = "wss://stream.binance.com:9443/ws"
    # TODO: abstract logic of intervals
    intervals = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"]
    rate_limits = [
        RateLimit("raw", limit=1200, time_interval=60),
        RateLimit(candles_endpoint, limit=1200, time_interval=60, linked_limits=[LinkedLimitWeightPair("raw", 1)]),
        RateLimit(health_check_endpoint, limit=1200, time_interval=60, linked_limits=[LinkedLimitWeightPair("raw", 1)])]
    columns = ["timestamp", "open", "low", "high", "close", "volume", "close_time",
               "quote_asset_volume", "n_trades", "taker_buy_base_volume", "taker_buy_quote_volume"]

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._bcf_logger is None:
            cls._bcf_logger = logging.getLogger(__name__)
        return cls._bcf_logger

    @classmethod
    def get_instance(cls) -> "BinanceCandlesFeed":
        if cls._binance_candles_shared_instance is None:
            cls._binance_candles_shared_instance = BinanceCandlesFeed()
        return cls._binance_candles_shared_instance

    def __init__(self, trading_pair: str, interval: str = "1m", update_interval: float = 60.0,
                 max_records: int = 150):
        super().__init__()
        self._ws_ready_event = asyncio.Event()
        self._shared_client: Optional[aiohttp.ClientSession] = None
        async_throttler = AsyncThrottler(
            rate_limits=self.rate_limits)
        self._api_factory = WebAssistantsFactory(throttler=async_throttler)

        self._trading_pair = trading_pair
        self._ex_trading_pair = trading_pair.replace("-", "")
        self._interval = interval
        self._check_network_interval = update_interval

        # TODO: check to remove
        self._ev_loop = asyncio.get_event_loop()
        self._candles = deque(maxlen=max_records)
        self._update_interval: float = update_interval
        self._fetch_candles_task: Optional[asyncio.Task] = None
        self._listen_candles_task: Optional[asyncio.Task] = None

    async def start_network(self):
        await self.stop_network()
        self._fill_candles_task = safe_ensure_future(self.fill_candles_loop())
        self._listen_candles_task = safe_ensure_future(self.listen_for_subscriptions())

    async def stop_network(self):
        if self._fetch_candles_task is not None:
            self._fetch_candles_task.cancel()
            self._fetch_candles_task = None
        if self._listen_candles_task is not None:
            self._listen_candles_task.cancel()
            self._listen_candles_task = None

    def start(self):
        NetworkBase.start(self)

    def stop(self):
        NetworkBase.stop(self)

    @property
    def is_ready(self):
        return len(self._candles) == self._candles.maxlen

    @property
    def name(self):
        return "binance_candles_api"

    @property
    def health_check_url(self):
        return self.base_url + self.health_check_endpoint

    @property
    def candles_url(self):
        return self.base_url + self.candles_endpoint

    async def check_network(self) -> NetworkStatus:
        rest_assistant = await self._api_factory.get_rest_assistant()
        await rest_assistant.execute_request(url=self.health_check_url,
                                             throttler_limit_id=self.health_check_endpoint)
        return NetworkStatus.CONNECTED

    @property
    def candles(self) -> pd.DataFrame:
        return pd.DataFrame(self._candles, columns=self.columns, dtype=float)

    async def fetch_candles(self,
                            start_time: Optional[int] = None,
                            end_time: Optional[int] = None,
                            limit: Optional[int] = 500):
        rest_assistant = await self._api_factory.get_rest_assistant()
        params = {"symbol": self._ex_trading_pair, "interval": self._interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        candles = await rest_assistant.execute_request(url=self.candles_url,
                                                       throttler_limit_id=self.candles_endpoint,
                                                       params=params)

        return np.array(candles)[:, :-1].astype(np.float)

    async def fill_candles_loop(self):
        while True:
            if self._ws_ready_event.is_set():
                missing_records = self._candles.maxlen - len(self._candles)
                if missing_records > 0:
                    end_timestamp = self._candles[0][0]
                    try:
                        # we have to add one more since, the last row is not going to be included
                        candles = await self.fetch_candles(end_time=end_timestamp, limit=missing_records + 1)
                        # we are computing again the quantity of records again since the websocket process is able to
                        # modify the deque and if we extend it, the new observations are going to be dropped.
                        missing_records = self._candles.maxlen - len(self._candles)
                        self._candles.extendleft(candles[-(missing_records + 1):-1][::-1])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.logger().exception(
                            "Unexpected error occurred when getting historical klines. Retrying in 1 seconds...",
                        )
            await self._sleep(1.0)

    async def listen_for_subscriptions(self):
        """
        Connects to the trade events and order diffs websocket endpoints and listens to the messages sent by the
        exchange. Each message is stored in its own queue.
        """
        ws: Optional[WSAssistant] = None
        while True:
            try:
                ws: WSAssistant = await self._connected_websocket_assistant()
                await self._subscribe_channels(ws)
                await self._process_websocket_messages(websocket_assistant=ws)
            except asyncio.CancelledError:
                raise
            except ConnectionError as connection_exception:
                self.logger().warning(f"The websocket connection was closed ({connection_exception})")
            except Exception:
                self.logger().exception(
                    "Unexpected error occurred when listening to public klines. Retrying in 5 seconds...",
                )
                await self._sleep(1.0)
            finally:
                await self._on_order_stream_interruption(websocket_assistant=ws)

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=self.wss_url,
                         ping_timeout=30)
        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the candles events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            candle_params = []
            candle_params.append(f"{self._ex_trading_pair.lower()}@kline_{self._interval}")
            payload = {
                "method": "SUBSCRIBE",
                "params": candle_params,
                "id": 1
            }
            subscribe_candles_request: WSJSONRequest = WSJSONRequest(payload=payload)

            await ws.send(subscribe_candles_request)
            self.logger().info("Subscribed to public klines...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to public klines...",
                exc_info=True
            )
            await self._ws_ready_event.wait()
            raise

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        async for ws_response in websocket_assistant.iter_messages():
            data: Dict[str, Any] = ws_response.data
            if data is not None and data.get("e") == "kline":  # data will be None when the websocket is disconnected
                timestamp = data["k"]["t"]
                open = data["k"]["o"]
                low = data["k"]["l"]
                high = data["k"]["h"]
                close = data["k"]["c"]
                volume = data["k"]["v"]
                close_ts = data["k"]["T"]
                quote_asset_volume = data["k"]["q"]
                n_trades = data["k"]["n"]
                taker_buy_base_volume = data["k"]["V"]
                taker_buy_quote_volume = data["k"]["Q"]
                if len(self._candles) == 0:
                    self._candles.append(np.array([timestamp, open, low, high, close, volume, close_ts,
                                                   quote_asset_volume, n_trades, taker_buy_base_volume,
                                                   taker_buy_quote_volume]))
                elif timestamp != int(self._candles[-1][0]):
                    self._candles.append(np.array([timestamp, open, low, high, close, volume, close_ts,
                                                   quote_asset_volume, n_trades, taker_buy_base_volume,
                                                   taker_buy_quote_volume]))

                self._ws_ready_event.set()

    async def _sleep(self, delay):
        """
        Function added only to facilitate patching the sleep in unit tests without affecting the asyncio module
        """
        await asyncio.sleep(delay)

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        websocket_assistant and await websocket_assistant.disconnect()
