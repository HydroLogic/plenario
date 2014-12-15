from flask import make_response, request, render_template, current_app, g, \
    Blueprint, abort, session as flask_session
from flask.ext.cache import Cache
from functools import update_wrapper
import os
import re
import math
from datetime import date, datetime, timedelta
from dateutil.parser import parse
from datetime_truncate import truncate
import time
import json
import string
from sqlalchemy import func, distinct, Column, Float, Table
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.types import NullType
from sqlalchemy.sql.expression import cast
from geoalchemy2 import Geometry
from operator import itemgetter
from itertools import groupby
from cStringIO import StringIO
import csv
from shapely.wkb import loads
from shapely.geometry import box, asShape
from collections import OrderedDict
from urlparse import urlparse

from plenario.models import MasterTable, MetaTable
from plenario.database import session, app_engine as engine, Base
from plenario.utils.helpers import get_socrata_data_info, slugify, increment_datetime_aggregate, send_mail
from plenario.utils.helpers import get_num_rows, get_num_weather_observations, get_num_rows_w_censusblocks
from plenario.tasks import add_dataset
from plenario.settings import CACHE_CONFIG

cache = Cache(config=CACHE_CONFIG)

API_VERSION = '/v1'
RESPONSE_LIMIT = 1000
CACHE_TIMEOUT = 60*60*6
VALID_DATA_TYPE = ['csv', 'json']
VALID_AGG = ['day', 'week', 'month', 'quarter', 'year']
METATABLE_KEYS_TO_EXCLUDE = [   'contributor_name', 'contributor_organization', 
                                'contributor_email', 'contributed_data_types', 
                                'is_socrata_source', 'approved_status']
WEATHER_COL_LOOKUP = {
    'daily': {
        'temp_lo': 'temp_min',
        'temp_hi': 'temp_max',
        'temp_avg': 'temp_avg',
        'precip_amount': 'precip_total',
    },
    'hourly': {
        'temp_lo': 'drybulb_fahrenheit',
        'temp_hi': 'drybulb_fahrenheit',
        'temp_avg': 'drybulb_fahrenheit',
        'precip_amount': 'hourly_precip',
    },
}

api = Blueprint('api', __name__)
dthandler = lambda obj: obj.isoformat() if isinstance(obj, date) else None

def crossdomain(origin=None, methods=None, headers=None,
                max_age=21600, attach_to_all=True,
                automatic_options=True): # pragma: no cover
    if methods is not None:
        methods = ', '.join(sorted(x.upper() for x in methods))
    if headers is not None and not isinstance(headers, basestring):
        headers = ', '.join(x.upper() for x in headers)
    if not isinstance(origin, basestring):
        origin = ', '.join(origin)
    if isinstance(max_age, timedelta):
        max_age = max_age.total_seconds()

    def get_methods():
        if methods is not None:
            return methods

        options_resp = current_app.make_default_options_response()
        return options_resp.headers['allow']

    def decorator(f):
        def wrapped_function(*args, **kwargs):
            if automatic_options and request.method == 'OPTIONS':
                resp = current_app.make_default_options_response()
            else:
                resp = make_response(f(*args, **kwargs))
            if not attach_to_all and request.method != 'OPTIONS':
                return resp

            h = resp.headers

            h['Access-Control-Allow-Origin'] = origin
            h['Access-Control-Allow-Methods'] = get_methods()
            h['Access-Control-Max-Age'] = str(max_age)
            if headers is not None:
                h['Access-Control-Allow-Headers'] = headers
            return resp

        f.provide_automatic_options = False
        return update_wrapper(wrapped_function, f)
    return decorator

def make_cache_key(*args, **kwargs):
    path = request.path
    args = str(hash(frozenset(request.args.items())))
    # print 'cache_key:', (path+args)
    return (path + args).encode('utf-8')

@api.route(API_VERSION + '/api/flush-cache')
def flush_cache():
    cache.clear()
    resp = make_response(json.dumps({'status' : 'ok', 'message' : 'cache flushed!'}))
    resp.headers['Content-Type'] = 'application/json'
    return resp

@api.route(API_VERSION + '/api/datasets')
#@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def meta():
    status_code = 200
    resp = {
            'meta': {
                'status': 'ok',
                'message': '',
            },
            'objects': []
        }
    dataset_name = request.args.get('dataset_name')
    num_rows = None
    num_weather_observations = None
    num_rows_w_censusblocks = None
    
    if dataset_name:
        metas = session.query(MetaTable)\
                       .filter(MetaTable.dataset_name == dataset_name)
    else:
        metas = session.query(MetaTable)

    metas=metas.filter(MetaTable.approved_status == 'true')

    print "metas are ", metas
    
    for m in metas.all():
        keys = m.as_dict()

        if dataset_name:
            curr_dataset_name = keys['dataset_name']

            num_rows = get_num_rows(curr_dataset_name)
            num_weather_observations = get_num_weather_observations(curr_dataset_name)
            num_rows_w_censusblocks = get_num_rows_w_censusblocks(curr_dataset_name)

            # If the caller specified specific datasets, add this extra info
            
            keys['num_rows'] = num_rows
            keys['num_weather_observations'] = num_weather_observations
            keys['num_rows_w_censusblocks'] = num_rows_w_censusblocks
        
        for e in METATABLE_KEYS_TO_EXCLUDE: del keys[e]
        resp['objects'].append(keys)
        
    resp['meta']['total'] = len(resp['objects'])
    resp = make_response(json.dumps(resp, default=dthandler), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@api.route(API_VERSION + '/api/fields/<dataset_name>/')
@cache.cached(timeout=CACHE_TIMEOUT)
@crossdomain(origin="*")
def dataset_fields(dataset_name):
    try:
        table = Table('dat_%s' % dataset_name, Base.metadata,
            autoload=True, autoload_with=engine,
            extend_existing=True)
        data = {
            'meta': {
                'status': 'ok',
                'message': '',
                'query': { 'dataset_name': dataset_name } 
            },
            'objects': []
        }
        status_code = 200
        table_exists = True
    except NoSuchTableError:
        table_exists = False
        data = {
            'meta': {
                'status': 'error',
                'message': "'%s' is not a valid table name" % dataset_name
            },
            'objects': []
        }
        status_code = 400
    if table_exists:
        fields = table.columns.keys()
        for col in table.columns:
            if not isinstance(col.type, NullType):
                d = {}
                d['field_name'] = col.name
                d['field_type'] = str(col.type)
                data['objects'].append(d)
    resp = make_response(json.dumps(data), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@api.route(API_VERSION + '/api/weather-stations/')
@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def weather_stations():
    raw_query_params = request.args.copy()
    stations_table = Table('weather_stations', Base.metadata, 
        autoload=True, autoload_with=engine, extend_existing=True)
    valid_query, query_clauses, resp, status_code = make_query(stations_table,raw_query_params)
    if valid_query:
        resp['meta']['status'] = 'ok'
        base_query = session.query(stations_table)
        for clause in query_clauses:
            base_query = base_query.filter(clause)
        values = [r for r in base_query.all()]
        fieldnames = [f for f in stations_table.columns.keys()]
        for value in values:
            d = {f:getattr(value, f) for f in fieldnames}
            loc = str(value.location)
            d['location'] = loads(loc.decode('hex')).__geo_interface__
            resp['objects'].append(d)
    resp['meta']['query'] = raw_query_params
    resp = make_response(json.dumps(resp, default=dthandler), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@api.route(API_VERSION + '/api/weather/<table>/')
@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def weather(table):
    raw_query_params = request.args.copy()

    weather_table = Table('dat_weather_observations_%s' % table, Base.metadata,
        autoload=True, autoload_with=engine, extend_existing=True)
    stations_table = Table('weather_stations', Base.metadata, 
        autoload=True, autoload_with=engine, extend_existing=True)
    valid_query, query_clauses, resp, status_code = make_query(weather_table,raw_query_params)
    if valid_query:
        resp['meta']['status'] = 'ok'
        base_query = session.query(weather_table, stations_table)\
            .join(stations_table, 
            weather_table.c.wban_code == stations_table.c.wban_code)
        for clause in query_clauses:
            base_query = base_query.filter(clause)

        try:
            base_query = base_query.order_by(getattr(weather_table.c, 'date').desc())
        except AttributeError:
            base_query = base_query.order_by(getattr(weather_table.c, 'datetime').desc())
        base_query = base_query.limit(RESPONSE_LIMIT) # returning the top 1000 records
        if raw_query_params.get('offset'):
            offset = raw_query_params['offset']
            base_query = base_query.offset(int(offset))
        values = [r for r in base_query.all()]
        weather_fields = weather_table.columns.keys()
        station_fields = stations_table.columns.keys()
        weather_data = {}
        station_data = {}
        for value in values:
            wd = {f: getattr(value, f) for f in weather_fields}
            sd = {f: getattr(value, f) for f in station_fields}
            if weather_data.get(value.wban_code):
                weather_data[value.wban_code].append(wd)
            else:
                weather_data[value.wban_code] = [wd]
            loc = str(value.location)
            sd['location'] = loads(loc.decode('hex')).__geo_interface__
            station_data[value.wban_code] = sd
        for station_id in weather_data.keys():
            d = {
                'station_info': station_data[station_id],
                'observations': weather_data[station_id],
            }
            resp['objects'].append(d)
        resp['meta']['total'] = sum([len(r['observations']) for r in resp['objects']])
    resp['meta']['query'] = raw_query_params
    resp = make_response(json.dumps(resp, default=dthandler), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp


@api.route(API_VERSION + '/api/timeseries/')
@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def dataset():
    raw_query_params = request.args.copy()

    # set default value for temporal aggregation
    agg = raw_query_params.get('agg')
    if not agg:
        agg = 'day'
    else:
        del raw_query_params['agg']

    # if no obs_date given, default to >= 90 days ago
    if not raw_query_params.get('obs_date__ge'):
        six_months_ago = datetime.now() - timedelta(days=90)
        raw_query_params['obs_date__ge'] = six_months_ago.strftime('%Y-%m-%d')

    if not raw_query_params.get('obs_date__le'):
        raw_query_params['obs_date__le'] = datetime.now().strftime('%Y-%m-%d') 

    # set datatype
    datatype = 'json'
    if raw_query_params.get('data_type'):
        datatype = raw_query_params['data_type']
        del raw_query_params['data_type']

    mt = MasterTable.__table__
    valid_query, query_clauses, resp, status_code = make_query(mt,raw_query_params)
    
    # check for valid output format
    if datatype not in VALID_DATA_TYPE:
        valid_query = False
        resp['meta']['status'] = 'error'
        resp['meta']['message'] = "'%s' is an invalid output format" % datatype
        resp = make_response(json.dumps(resp, default=dthandler), 400)
        resp.headers['Content-Type'] = 'application/json'

    # check for valid temporal aggregate
    if agg not in VALID_AGG:
        valid_query = False
        resp['meta']['status'] = 'error'
        resp['meta']['message'] = "'%s' is an invalid temporal aggregation" % agg
        resp = make_response(json.dumps(resp, default=dthandler), 400)
        resp.headers['Content-Type'] = 'application/json'

    if valid_query:
        time_agg = func.date_trunc(agg, mt.c['obs_date'])
        base_query = session.query(time_agg, 
            func.count(mt.c['obs_date']),
            mt.c['dataset_name'])
        base_query = base_query.filter(mt.c['current_flag'] == True)
        for clause in query_clauses:
            base_query = base_query.filter(clause)
        base_query = base_query.group_by(mt.c['dataset_name'])\
            .group_by(time_agg)\
            .order_by(time_agg)
        values = [o for o in base_query.all()]

        # init from and to dates ad python datetimes
        from_date = truncate(parse(raw_query_params['obs_date__ge']), agg)
        if 'obs_date__le' in raw_query_params.keys():
            to_date = parse(raw_query_params['obs_date__le'])
        else:
            to_date = datetime.now()

        # build the response
        results = sorted(values, key=itemgetter(2))
        for k,g in groupby(results, key=itemgetter(2)):
            d = {'dataset_name': k}

            items = []
            dense_matrix = []
            cursor = from_date
            v_index = 0
            dataset_values = list(g)
            while cursor <= to_date:
                if v_index < len(dataset_values) and \
                    dataset_values[v_index][0].replace(tzinfo=None) == cursor:
                    dense_matrix.append((cursor, dataset_values[v_index][1]))
                    v_index += 1
                else:
                    dense_matrix.append((cursor, 0))

                cursor = increment_datetime_aggregate(cursor, agg)

            dense_matrix = OrderedDict(dense_matrix)
            for k in dense_matrix:
                i = {
                    'datetime': k,
                    'count': dense_matrix[k],
                    }
                items.append(i)

            d['items'] = items
            resp['objects'].append(d)

        resp['meta']['query'] = raw_query_params
        loc = resp['meta']['query'].get('location_geom__within')
        if loc:
            resp['meta']['query']['location_geom__within'] = json.loads(loc)
        resp['meta']['query']['agg'] = agg
        resp['meta']['status'] = 'ok'
    
        if datatype == 'json':
            resp = make_response(json.dumps(resp, default=dthandler), status_code)
            resp.headers['Content-Type'] = 'application/json'
        elif datatype == 'csv':
 
            # response format
            # temporal_group,dataset_name_1,dataset_name_2
            # 2014-02-24 00:00:00,235,653
            # 2014-03-03 00:00:00,156,624
 
            fields = ['temporal_group']
            for o in resp['objects']:
                fields.append(o['dataset_name'])
 
            csv_resp = []
            i = 0
            for k,g in groupby(resp['objects'], key=itemgetter('dataset_name')):
                l_g = list(g)[0]
                
                j = 0
                for row in l_g['items']:
                    # first iteration, populate the first column with temporal_groups
                    if i == 0: 
                        csv_resp.append([row['datetime']])
                    csv_resp[j].append(row['count'])
                    j += 1
                i += 1
                    
            csv_resp.insert(0, fields)
            csv_resp = make_csv(csv_resp)
            resp = make_response(csv_resp, 200)
            resp.headers['Content-Type'] = 'text/csv'
            filedate = datetime.now().strftime('%Y-%m-%d')
            resp.headers['Content-Disposition'] = 'attachment; filename=%s.csv' % (filedate)
    return resp

@api.route(API_VERSION + '/api/detail/')
@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def detail():
    raw_query_params = request.args.copy()
    # if no obs_date given, default to >= 30 days ago
    obs_dates = [i for i in raw_query_params.keys() if i.startswith('obs_date')]
    if not obs_dates:
        six_months_ago = datetime.now() - timedelta(days=30)
        raw_query_params['obs_date__ge'] = six_months_ago.strftime('%Y-%m-%d')
    
    include_weather = False
    if raw_query_params.get('weather') is not None:
        include_weather = raw_query_params['weather']
        del raw_query_params['weather']
    agg, datatype, queries = parse_join_query(raw_query_params)
    order_by = raw_query_params.get('order_by')
    offset = raw_query_params.get('offset')
    mt = MasterTable.__table__
    valid_query, base_clauses, resp, status_code = make_query(mt, queries['base'])
    if not raw_query_params.get('dataset_name'):
        valid_query = False
        resp['meta'] = {
            'status': 'error',
            'message': "'dataset_name' is required"
        }
        resp['objects'] = []
    if valid_query:
        resp['meta']['status'] = 'ok'
        dname = raw_query_params['dataset_name']
        dataset = Table('dat_%s' % dname, Base.metadata,
            autoload=True, autoload_with=engine,
            extend_existing=True)
        dataset_fields = dataset.columns.keys()
        base_query = session.query(mt, dataset)
        if include_weather:
            date_col_name = 'date'
            try:
                date_col_name = slugify(session.query(MetaTable)\
                    .filter(MetaTable.dataset_name == dname)\
                    .first().observed_date)
            except AttributeError:
                pass
            date_col_type = str(getattr(dataset.c, date_col_name).type).lower()
            if 'timestamp' in date_col_type:
                weather_tname = 'hourly'
            else:
                weather_tname = 'daily'
            weather_table = Table('dat_weather_observations_%s' % weather_tname, Base.metadata, 
                autoload=True, autoload_with=engine, extend_existing=True)
            weather_fields = weather_table.columns.keys()
            base_query = session.query(mt, dataset, weather_table)
        valid_query, detail_clauses, resp, status_code = make_query(dataset, queries['detail'])
        if valid_query:
            resp['meta']['status'] = 'ok'
            pk = [p.name for p in dataset.primary_key][0]
            base_query = base_query.join(dataset, mt.c.dataset_row_id == dataset.c[pk])
            for clause in base_clauses:
                base_query = base_query.filter(clause)
            for clause in detail_clauses:
                base_query = base_query.filter(clause)
            if include_weather:
                w_q = {}
                if queries['weather']:
                    for k,v in queries['weather'].items():
                        try:
                            fname, operator = k.split('__')
                        except ValueError:
                            operator = 'eq'
                            pass
                        t_fname = WEATHER_COL_LOOKUP[weather_tname].get(fname, fname)
                        w_q['__'.join([t_fname, operator])] = v
                valid_query, weather_clauses, resp, status_code = make_query(weather_table, w_q)
                if valid_query:
                    resp['meta']['status'] = 'ok'
                    base_query = base_query.join(weather_table, mt.c.weather_observation_id == weather_table.c.id)
                    for clause in weather_clauses:
                        base_query = base_query.filter(clause)
            if valid_query:
                if order_by:
                    col, order = order_by.split(',')
                    base_query = base_query.order_by(getattr(mt.c[col], order)())
                else:
                    base_query = base_query.order_by(mt.c.master_row_id.asc())
                base_query = base_query.limit(RESPONSE_LIMIT)
                if offset:
                    base_query = base_query.offset(int(offset))
                values = [r for r in base_query.all()]
                for value in values:
                    d = {f:getattr(value, f) for f in dataset_fields}
                    if include_weather:
                        d = {
                            'observation': {f:getattr(value, f) for f in dataset_fields},
                            'weather': {f:getattr(value, f) for f in weather_fields},
                        }
                    resp['objects'].append(d)
                resp['meta']['query'] = raw_query_params
                loc = resp['meta']['query'].get('location_geom__within')
                if loc:
                    resp['meta']['query']['location_geom__within'] = json.loads(loc)
                resp['meta']['total'] = len(resp['objects'])
    if datatype == 'json':
        resp = make_response(json.dumps(resp, default=dthandler), status_code)
        resp.headers['Content-Type'] = 'application/json'
    elif datatype == 'csv':
        csv_resp = [dataset_fields]
        if include_weather:
            csv_resp = [dataset_fields + weather_fields]
        for value in values:
            d = [getattr(value, f) for f in dataset_fields]
            if include_weather:
                d.extend([getattr(value, f) for f in weather_fields])
            csv_resp.append(d)
        resp = make_response(make_csv(csv_resp), 200)
        filedate = datetime.now().strftime('%Y-%m-%d')
        dname = raw_query_params['dataset_name']
        filedate = datetime.now().strftime('%Y-%m-%d')
        resp.headers['Content-Type'] = 'text/csv'
        resp.headers['Content-Disposition'] = 'attachment; filename=%s_%s.csv' % (dname, filedate)
    return resp

@api.route(API_VERSION + '/api/detail-aggregate/')
@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def detail_aggregate():
    raw_query_params = request.args.copy()
    agg, datatype, queries = parse_join_query(raw_query_params)
    if not agg:
        agg = 'day'

    # if no obs_date given, default to >= 90 days ago
    if not raw_query_params.get('obs_date__ge'):
        six_months_ago = datetime.now() - timedelta(days=90)
        raw_query_params['obs_date__ge'] = six_months_ago.strftime('%Y-%m-%d')

    if not raw_query_params.get('obs_date__le'):
        raw_query_params['obs_date__le'] = datetime.now().strftime('%Y-%m-%d') 

    mt = MasterTable.__table__
    valid_query, base_clauses, resp, status_code = make_query(mt, queries['base'])

    # check for valid output format
    if datatype not in VALID_DATA_TYPE:
        valid_query = False
        resp['meta']['status'] = 'error'
        resp['meta']['message'] = "'%s' is an invalid output format" % datatype
        resp = make_response(json.dumps(resp, default=dthandler), 400)
        resp.headers['Content-Type'] = 'application/json'

    # check for valid temporal aggregate
    if agg not in VALID_AGG:
        valid_query = False
        resp['meta']['status'] = 'error'
        resp['meta']['message'] = "'%s' is an invalid temporal aggregation" % agg
        resp = make_response(json.dumps(resp, default=dthandler), 400)
        resp.headers['Content-Type'] = 'application/json'

    if valid_query:
        time_agg = func.date_trunc(agg, mt.c['obs_date'])
        base_query = session.query(time_agg, func.count(mt.c.dataset_row_id))
        dname = raw_query_params.get('dataset_name')

        try:
            dataset = Table('dat_%s' % dname, Base.metadata,
                autoload=True, autoload_with=engine,
                extend_existing=True)
            valid_query, detail_clauses, resp, status_code = make_query(dataset, queries['detail'])
        except:
            valid_query = False
            resp['meta']['status'] = 'error'
            if not dname:
                resp['meta']['message'] = "dataset_name' is required"
            else:
                resp['meta']['message'] = "unable to find dataset '%s'" % dname
            resp = make_response(json.dumps(resp, default=dthandler), 400)
            resp.headers['Content-Type'] = 'application/json'

        if valid_query:
            pk = [p.name for p in dataset.primary_key][0]
            base_query = base_query.join(dataset, mt.c.dataset_row_id == dataset.c[pk])
            for clause in base_clauses:
                base_query = base_query.filter(clause)
            for clause in detail_clauses:
                base_query = base_query.filter(clause)
            values = [r for r in base_query.group_by(time_agg).order_by(time_agg).all()]
            
            # init from and to dates ad python datetimes
            from_date = truncate(parse(raw_query_params['obs_date__ge']), agg)
            if 'obs_date__le' in raw_query_params.keys():
                to_date = parse(raw_query_params['obs_date__le'])
            else:
                to_date = datetime.now()

            items = []
            dense_matrix = []
            cursor = from_date
            v_index = 0
            while cursor <= to_date:
                if v_index < len(values) and \
                    values[v_index][0].replace(tzinfo=None) == cursor:
                    dense_matrix.append((cursor, values[v_index][1]))
                    v_index += 1
                else:
                    dense_matrix.append((cursor, 0))

                cursor = increment_datetime_aggregate(cursor, agg)

            dense_matrix = OrderedDict(dense_matrix)
            for k in dense_matrix:
                i = {
                    'datetime': k,
                    'count': dense_matrix[k],
                    }
                items.append(i)

            if datatype == 'json':
                resp['objects'] = items
                resp['meta']['status'] = 'ok'
                resp['meta']['query'] = raw_query_params
                loc = resp['meta']['query'].get('location_geom__within')
                if loc:
                    resp['meta']['query']['location_geom__within'] = json.loads(loc)
                resp['meta']['query']['agg'] = agg

                resp = make_response(json.dumps(resp, default=dthandler), status_code)
                resp.headers['Content-Type'] = 'application/json'
            elif datatype == 'csv':
                outp = StringIO()
                writer = csv.DictWriter(outp, fieldnames=items[0].keys())
                writer.writeheader()
                writer.writerows(items)
                resp = make_response(outp.getvalue(), status_code)
                resp.headers['Content-Type'] = 'text/csv'
                filedate = datetime.now().strftime('%Y-%m-%d')
                resp.headers['Content-Disposition'] = 'attachment; filename=%s.csv' % (filedate)
    return resp

@api.route(API_VERSION + '/api/grid/')
@cache.cached(timeout=CACHE_TIMEOUT, key_prefix=make_cache_key)
@crossdomain(origin="*")
def grid():
    raw_query_params = request.args.copy()

    buff = request.args.get('buffer', 100)
    
    resolution = request.args.get('resolution')
    if not resolution:
        resolution = 500
    else:
        del raw_query_params['resolution']
    
    center = request.args.getlist('center[]')
    if not center:
        center = [41.880517,-87.644061]
    else:
        del raw_query_params['center[]']
    location_geom = request.args.get('location_geom__within')

    if raw_query_params.get('buffer'):
        del raw_query_params['buffer']

    agg, datatype, queries = parse_join_query(raw_query_params)

    size_x, size_y = getSizeInDegrees(float(resolution), float(center[0]))
    if location_geom:
        location_geom = json.loads(location_geom)['geometry']
        if location_geom['type'] == 'LineString':
            shape = asShape(location_geom)
            lat = shape.centroid.y
            # 100 meters by default
            x, y = getSizeInDegrees(int(buff), lat)
            size_x, size_y = getSizeInDegrees(50, lat)
            location_geom = shape.buffer(y).__geo_interface__
        location_geom['crs'] = {"type":"name","properties":{"name":"EPSG:4326"}}
    mt = MasterTable.__table__
    valid_query, base_clauses, resp, status_code = make_query(mt, queries['base'])

    if valid_query:
        base_query = session.query(func.count(mt.c.dataset_row_id), 
                func.ST_SnapToGrid(mt.c.location_geom, size_x, size_y))
        dname = raw_query_params['dataset_name']
        dataset = Table('dat_%s' % dname, Base.metadata,
            autoload=True, autoload_with=engine,
            extend_existing=True)
        valid_query, detail_clauses, resp, status_code = make_query(dataset, queries['detail'])
        if valid_query:
            pk = [p.name for p in dataset.primary_key][0]
            base_query = base_query.join(dataset, mt.c.dataset_row_id == dataset.c[pk])
            for clause in base_clauses:
                base_query = base_query.filter(clause)
            for clause in detail_clauses:
                base_query = base_query.filter(clause)

            base_query = base_query.group_by(func.ST_SnapToGrid(mt.c.location_geom, size_x, size_y))
            values = [d for d in base_query.all()]
            resp = {'type': 'FeatureCollection', 'features': []}
            for value in values:
                d = {
                    'type': 'Feature', 
                    'properties': {
                        'count': value[0], 
                    },
                }
                if value[1]:
                    pt = loads(value[1].decode('hex'))
                    south, west = (pt.x - (size_x / 2)), (pt.y - (size_y /2))
                    north, east = (pt.x + (size_x / 2)), (pt.y + (size_y / 2))
                    d['geometry'] = box(south, west, north, east).__geo_interface__
                
                resp['features'].append(d)
    
    resp = make_response(json.dumps(resp, default=dthandler), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

# helper functions
def make_query(table, raw_query_params):
    table_keys = table.columns.keys()
    args_keys = raw_query_params.keys()
    resp = {
        'meta': {
            'status': 'error',
            'message': '',
        },
        'objects': [],
    }
    status_code = 200
    query_clauses = []
    valid_query = True
    if 'offset' in args_keys:
        args_keys.remove('offset')
    if 'limit' in args_keys:
        args_keys.remove('limit')
    if 'order_by' in args_keys:
        args_keys.remove('order_by')
    if 'weather' in args_keys:
        args_keys.remove('weather')
    for query_param in args_keys:
        try:
            field, operator = query_param.split('__')
        except ValueError:
            field = query_param
            operator = 'eq'
        query_value = raw_query_params.get(query_param)
        column = table.columns.get(field)
        if field not in table_keys:
            resp['meta']['message'] = '"%s" is not a valid fieldname' % field
            status_code = 400
            valid_query = False
        elif operator == 'in':
            query = column.in_(query_value.split(','))
            query_clauses.append(query)
        elif operator == 'within':
            geo = json.loads(query_value)
            if 'features' in geo.keys():
                val = geo['features'][0]['geometry']
            elif 'geometry' in geo.keys():
                val = geo['geometry']
            else:
                val = geo
            if val['type'] == 'LineString':
                shape = asShape(val)
                lat = shape.centroid.y
                # 100 meters by default
                x, y = getSizeInDegrees(100, lat)
                val = shape.buffer(y).__geo_interface__
            val['crs'] = {"type":"name","properties":{"name":"EPSG:4326"}}
            query = column.ST_Within(func.ST_GeomFromGeoJSON(json.dumps(val)))
            query_clauses.append(query)
        elif operator.startswith('time_of_day'):
            if operator.endswith('ge'):
                query = func.date_part('hour', column).__ge__(query_value)
            elif operator.endswith('le'):
                query = func.date_part('hour', column).__le__(query_value)
            query_clauses.append(query)
        else:
            try:
                attr = filter(
                    lambda e: hasattr(column, e % operator),
                    ['%s', '%s_', '__%s__']
                )[0] % operator
            except IndexError:
                resp['meta']['message'] = '"%s" is not a valid query operator' % operator
                status_code = 400
                valid_query = False
                break
            if query_value == 'null': # pragma: no cover
                query_value = None
            query = getattr(column, attr)(query_value)
            query_clauses.append(query)
            
    return valid_query, query_clauses, resp, status_code

def getSizeInDegrees(meters, latitude):

    earth_circumference = 40041000.0 # meters, average circumference
    degrees_per_meter = 360.0 / earth_circumference
    
    degrees_at_equator = meters * degrees_per_meter

    latitude_correction = 1.0 / math.cos(latitude * (math.pi / 180.0))
    
    degrees_x = degrees_at_equator * latitude_correction
    degrees_y = degrees_at_equator

    return degrees_x, degrees_y

def make_csv(data):
    outp = StringIO()
    writer = csv.writer(outp)
    writer.writerows(data)
    return outp.getvalue()

def parse_join_query(params):
    queries = {
        'base' : {},
        'detail': {},
        'weather': {},
    }
    agg = 'day'
    datatype = 'json'
    master_columns = [
        'obs_date', 
        'location_geom', 
        'dataset_name',
        'weather_observation_id',
        'census_block',
    ]
    weather_columns = [
        'temp_hi',
        'temp_lo',
        'temp_avg',
        'precip_amount',
    ]
    for key, value in params.items():
        if key.split('__')[0] in master_columns:
            queries['base'][key] = value
        elif key.split('__')[0] in weather_columns:
            queries['weather'][key] = value
        elif key == 'agg':
            agg = value
        elif key == 'data_type':
            datatype = value.lower()
        else:
            queries['detail'][key] = value
    return agg, datatype, queries
