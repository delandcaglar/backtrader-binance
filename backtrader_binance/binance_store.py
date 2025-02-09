from functools import wraps
from math import floor
import time

from backtrader.dataseries import TimeFrame
from backtrader.metabase import MetaParams
from backtrader.utils.py3 import with_metaclass
from binance.client import Client
from binance.websockets import BinanceSocketManager
from binance.enums import *
from binance.exceptions import BinanceAPIException
from twisted.internet import reactor


class MetaSingleton(MetaParams):
    """Metaclass to make a metaclassed class a singleton"""
    def __init__(cls, name, bases, dct):
        super(MetaSingleton, cls).__init__(name, bases, dct)
        cls._singleton = None

    def __call__(cls, *args, **kwargs):
        if cls._singleton is None:
            cls._singleton = (
                super(MetaSingleton, cls).__call__(*args, **kwargs))

        return cls._singleton


class BinanceStore(with_metaclass(MetaSingleton, object)):
    _GRANULARITIES = {
        (TimeFrame.Minutes, 1): '1m',
        (TimeFrame.Minutes, 3): '3m',
        (TimeFrame.Minutes, 5): '5m',
        (TimeFrame.Minutes, 15): '15m',
        (TimeFrame.Minutes, 30): '30m',
        (TimeFrame.Minutes, 60): '1h',
        (TimeFrame.Minutes, 120): '2h',
        (TimeFrame.Minutes, 240): '4h',
        (TimeFrame.Minutes, 360): '6h',
        (TimeFrame.Minutes, 480): '8h',
        (TimeFrame.Minutes, 720): '12h',
        (TimeFrame.Days, 1): '1d',
        (TimeFrame.Days, 3): '3d',
        (TimeFrame.Weeks, 1): '1w',
        (TimeFrame.Months, 1): '1M',
    }

    BrokerCls = None  # Broker class will autoregister
    DataCls = None  # Data class will auto register

    @classmethod
    def getdata(cls, *args, **kwargs):
        """Returns ``DataCls`` with args, kwargs"""
        return cls.DataCls(*args, **kwargs)

    @classmethod
    def getbroker(cls, *args, **kwargs):
        """Returns broker with *args, **kwargs from registered ``BrokerCls``"""
        return cls.BrokerCls(*args, **kwargs)

    def __init__(self, api_key, api_secret, coin_refer, coin_target, retries=5):
        self.binance = Client(api_key, api_secret)
        self.binance_socket = BinanceSocketManager(self.binance)
        self.coin_refer = coin_refer
        self.coin_target = coin_target
        self.retries = retries

        self._precision = None
        self._step_size = None

        self._cash = 0
        self._value = 0
        self.get_balance()
        
    def retry(method):
        @wraps(method)
        def retry_method(self, *args, **kwargs):
            for i in range(self.retries):
                time.sleep(500 / 1000)  # Rate limit
                try:
                    return method(self, *args, **kwargs)
                except BinanceAPIException:
                    if i == self.retries - 1:
                        raise

        return retry_method

    @retry
    def cancel_order(self, order_id):
        try:
            self.binance.cancel_order(symbol=self.symbol, orderId=order_id)
        except BinanceAPIException as api_err:
            if api_err.code == -2011:  # Order filled
                return
            else:
                raise api_err
        except Exception as err:
            raise err
    
    @retry
    def create_order(self, side, type, size, price):
        return self.binance.create_order(
            symbol=self.symbol,
            side=side,
            type=type,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=self.format_quantity(size),
            price=self.strprecision(price))
    
    @retry
    def close_open_orders(self):
        orders = self.binance.get_open_orders(symbol=self.symbol)
        for o in orders:
            self.cancel_order(o['orderId'])

    def format_quantity(self, size):
        precision = self.step_size.find('1') - 1
        if precision > 0:
            return '{:0.0{}f}'.format(size, precision)
        return floor(int(size))

    @retry
    def get_asset_balance(self, asset):
        balance = self.binance.get_asset_balance(asset)
        return float(balance['free']), float(balance['locked'])

    def get_balance(self):
        free, locked = self.get_asset_balance(self.coin_target)
        self._cash = free
        self._value = free + locked

    def get_interval(self, timeframe, compression):
        return self._GRANULARITIES.get((timeframe, compression))

    def get_precision(self):
        symbol_info = self.get_symbol_info(self.symbol)
        self._precision = symbol_info['baseAssetPrecision']

    def get_step_size(self):
        symbol_info = self.get_symbol_info(self.symbol)
        for f in symbol_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                self._step_size = f['stepSize']

    @retry
    def get_symbol_info(self, symbol):
        return self.binance.get_symbol_info(symbol)

    @property
    def precision(self):
        if not self._precision:
            self.get_precision()
        return self._precision
    
    def start_socket(self):
        if self.binance_socket.is_alive():
            return

        self.binance_socket.daemon = True
        self.binance_socket.start()

    @property
    def step_size(self):
        if not self._step_size:
            self.get_step_size()

        return self._step_size

    def stop_socket(self):
        self.binance_socket.close()
        reactor.stop()
        self.binance_socket.join()

    def strprecision(self, value):
        return '{:.{}f}'.format(value, self.precision)

    @property
    def symbol(self):
        return '{}{}'.format(self.coin_refer, self.coin_target)
