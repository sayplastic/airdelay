# encoding=utf-8

from __future__ import print_function
from datetime import datetime
import json
from random import randint
import sys
import time
import traceback
from functools import partial
from urlparse import urlparse

from bs4 import BeautifulSoup
from concurrent import futures
import requests
from requests_futures.sessions import FuturesSession
import redis

from codes import find_airport_code, find_airport_name


r = redis.StrictRedis()


_agents = [
    'Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; Win64; x64; Trident/6.0)',
    'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.1; Win64; x64; Trident/4.0; .NET CLR 2.0.50727; SLCC2;'
    ' .NET CLR 3.5.30729; .NET CLR 3.0.30729; Media Center PC 6.0; Tablet PC 2.0)',
    'Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 5.1; GTB6.4; .NET CLR 1.1.4322; FDM; .NET CLR 2.0.50727;'
    ' .NET CLR 3.0.04506.30; .NET CLR 3.0.4506.2152; .NET CLR 3.5.30729)',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_6_8) AppleWebKit/534.57.2 (KHTML, like Gecko)'
    ' Version/5.1.7 Safari/534.57.2',
    'Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US) AppleWebKit/531.21.8 (KHTML, like Gecko)'
    ' Version/4.0.4 Safari/531.21.10',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:21.0) Gecko/20100101 Firefox/21.0',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:20.0) Gecko/20100101 Firefox/20.0',
    'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.31 (KHTML, like Gecko) Chrome/26.0.1410.64 Safari/537.31',
    'Mozilla/5.0 (X11; Linux i686) AppleWebKit/537.22 (KHTML, like Gecko) Ubuntu Chromium/25.0.1364.160'
    ' Chrome/25.0.1364.160 Safari/537.22',
]


class FlightStatus(object):
    SCHEDULED = 'scheduled'
    DELAYED = 'delayed'
    DEPARTED = 'departed'
    LANDED = 'landed'
    CANCELLED = 'cancelled'
    ARRIVED = 'arrived'


class FlightEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()

        return super(FlightEncoder, self).default(o)


def flight_decoder(dct):
    '''Decodes flight entries to Flight objects, leaves other dicts as is'''
    if 'origin' in dct:
        return Flight(**dct)
    return dct


class Flight(dict):
    '''Enhanced dictionary for holding timetable entries'''
    fields = ['origin', 'origin_name', 'destination', 'destination_name', 'number', 'airline',
              'time_scheduled', 'time_actual', 'status', 'is_codeshare']
    time_fields = ['time_scheduled', 'time_actual']

    def __init__(self, **kwargs):
        clean_data = self.clean(self._clean_kwargs(kwargs))
        strict_data = {f: clean_data[f] for f in set(self.fields) & set(clean_data.keys())}
        super(Flight, self).__init__(**strict_data)

    def __getattr__(self, item):
        if item in self.fields:
            return self.get(item, None)
        else:
            raise AttributeError(item)

    def __setattr__(self, key, value):
        if key in self.fields:
            self[key] = self.clean_value(key, value)
        else:
            raise AttributeError(key)

    def set_origin(self, name, iata_code=None):
        self.origin_name = name
        self.origin = iata_code or find_airport_code(name)

    def set_destination(self, name, iata_code=None):
        self.destination_name = name
        self.destination = iata_code or find_airport_code(name)

    @staticmethod
    def _clean_kwargs(kwargs):
        return dict(filter(lambda item: not(item[1] == ''), kwargs.items()))

    def clean(self, data):
        return {k: self.clean_value(k, v) for k, v in data.items()}

    def clean_value(self, key, value):
        if key in self.time_fields and isinstance(value, basestring):
            value = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S')
        return value


class Timetable(object):
    '''Holds Flights collection and a bit of metadata'''
    iata_code = None
    name = None
    time_retrieved = None
    cache_timeout = 180
    flights = None

    def __init__(self, iata_code, *args, **kwargs):
        self.iata_code = iata_code
        self.flights = []
        self.name = find_airport_name(iata_code)
        self.time_retrieved = datetime.now().replace(microsecond=0)
        super(Timetable, self).__init__(*args, **kwargs)

    @property
    def _cache_key(self):
        return 'airport_cache:' + self.iata_code

    def is_in_cache(self):
        return r.exists(self._cache_key)

    def get_raw_from_cache(self):
        return r.get(self._cache_key)

    def load_from_cache(self):
        cached_timetable = self.get_raw_from_cache()
        return self.set_from_json(cached_timetable)

    def save_to_cache(self):
        if self.is_in_cache():
            return
        r.set(self._cache_key, self.to_json())
        r.expire(self._cache_key, self.cache_timeout)

    def to_dict(self):
        return {
            'iata_code': self.iata_code,
            'name': self.name,
            'time_retrieved': self.time_retrieved,
            'flights': self.flights
        }

    def set_from_json(self, raw):
        try:
            loaded_timetable = self.from_json(raw)
            self.flights = loaded_timetable['flights']
            self.time_retrieved = loaded_timetable['time_retrieved']
        except (ValueError, TypeError, KeyError):
            return False
        return True

    @classmethod
    def from_json(cls, json_string):
        return json.loads(json_string, object_hook=flight_decoder)

    def to_json(self):
        return json.dumps(self.to_dict(), cls=FlightEncoder)

    def __add__(self, other):
        self.flights.extend(other)
        return self


class Throttler(object):
    def __init__(self, delay=2):
        self.delay = delay
        self.domains_last_called = {}

    def get_domain(self, url):
        return urlparse(url).netloc

    def get_last_called(self, url):
        domain = self.get_domain(url)
        return self.domains_last_called.get(domain, None) if domain else None

    def set_last_called(self, url, time_):
        domain = self.get_domain(url)
        if domain:
            self.domains_last_called[domain] = time_

    def sleep(self):
        time.sleep(self.delay)

    def __call__(self, func, *args, **kwargs):
        def wrapper(self_, url, *func_args, **func_kwargs):
            last_called = self.get_last_called(url)
            now = time.time()
            self.set_last_called(url, now)
            print('last_called: {}, now: {}'.format(last_called, now))
            if last_called and now - last_called < self.delay:
                print('delayin!!')
                self.sleep()
            return func(self_, url, *func_args, **func_kwargs)
        return wrapper


throttle_requests = Throttler()


class BaseParser(object):
    '''
    Base class for all parsers containing asynchronous running methods.
    Inheriting classes need to define only one method:
    def parse(self, **defaults)
        ...
        yield Flight(...)
    '''
    iata_code = None
    name = None
    urls = None
    client = None
    request_headers = {
        'Accept-Language': 'en-US',
    }

    def __init__(self, iata_code):
        self.records = Timetable(iata_code)
        self.iata_code = iata_code
        self.name = find_airport_name(iata_code)

        self.metadata = {
            'status': None,
            'iata_code': iata_code,
            'name': self.name,
            'flights': self.records
        }

        self.request_headers['User-Agent'] = _agents[randint(0, len(_agents) - 1)]

    def get_request_headers(self):
        return self.request_headers

    def set_status(self, value):
        self.metadata['status'] = value

    # @throttle_requests
    def fetch_url(self, url):
        return requests.get(url, headers=self.get_request_headers())

    def parse_html(self, response):
        # Tornado Async client or Requests or just plain html
        if isinstance(response, basestring):
            html = response
        else:
            html = getattr(response, 'body', response.content)
        return BeautifulSoup(html)

    def parse_async(self, content, **defaults):
        try:
            self.records += list(self.parse(self.parse_html(content), **defaults))
        except:
            print('error while parsing {}:\n'.format(self.iata_code))
            traceback.print_exception(*sys.exc_info())

    def run(self):
        if self.records.load_from_cache():
            return self.records

        for type_, urls in self.urls.items():
            for results in map(lambda url: list(self.parse(self.parse_html(self.fetch_url(url)), type=type_)), urls):
                self.records += results

        # self.set_status('OK')
        self.records.save_to_cache()
        return self.records

    def get_async_parsers(self):
        fetchers = {}
        with futures.ThreadPoolExecutor(max_workers=6) as executor:
            session = FuturesSession(executor)
            session.headers.update(self.get_request_headers())
            # getter = partial(session.request, 'get',
            #                  background_callback=lambda s, r: self.parse_html(r))

            for type_, urls in self.urls.items():
                fetchers.update(map(lambda url: (executor.submit(self.fetch_url, url), type_), urls))

            parsers = [executor.submit(self.parse_async, fetcher.result(), type=fetchers[fetcher])
                       for fetcher in futures.as_completed(fetchers)]
        return parsers

    def get_async_results(self, retrievers=None):
        """Waits while data retrieval futures finish their job or returns records right away"""
        if retrievers:
            futures.wait(retrievers)
        return self.records

    def run_async(self):
        def results_retrieval():
            with futures.ThreadPoolExecutor(max_workers=2) as executor:
                cache_hit = executor.submit(self.records.load_from_cache)
                if not cache_hit.result():
                    future_results = executor.submit(self.get_async_results, self.get_async_parsers())
                    future_results.add_done_callback(lambda f: self.records.save_to_cache())
                    return future_results.result()
                else:
                    return self.get_async_results()

        with futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(results_retrieval)

    def parse(self, content, **defaults):
        raise NotImplementedError