# encoding=utf-8

import csv
import redis


def find_airport_code(name):
    return get_cache().get(lk(name))


def find_airport_name(code):
    return get_cache().hget('airport:' + code, 'name')


def make_lookup_key(name):
    return 'airport_lookup:' + name.lower()
lk = make_lookup_key


def get_connection():
    return redis.StrictRedis()


def is_cached(r):
    try:
        return int(r.get(lk('__cached')))
    except TypeError:
        return 0


def get_cache():
    r = get_connection()

    if not is_cached(r):
        cache_airports(r, load_airports())
        r.set(lk('__cached'), 1)

    return r


def cache_airports(r, airports):
    for port in airports:
        city_key = lk(port['city'])
        name_key = lk(port['name'])
        r.set(city_key, port['iata_code'])
        if city_key != name_key:
            r.set(name_key, port['iata_code'])

        r.hmset('airport:' + port['iata_code'], port)


def reload_airports_cache():
    get_connection().set(lk('__cached'), 0)
    get_cache()


def load_airports(filename='airports.dat'):
    fields = 'id name city country iata_code icao_code latitude longitude altitude_ft timezone dst'.split()

    with open(filename, 'rb') as airports_file:
        airports_csv = csv.DictReader(airports_file, fieldnames=fields, delimiter=',')
        for port in airports_csv:
            yield port