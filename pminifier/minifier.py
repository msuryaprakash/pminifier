"""
Maintain a collection of URL/ID pairs, to allow for 'shortening' a URL.
    Ex: http://www.google.com/  --->  3294
    Also allow for URLs to be represented as a url friendly string.
        Ex: 329423322  --->  'cZ1Ya'

        Unit tests can be found in minifier_tests.py
"""
import pymongo
import math
import md5

import logging
from pymongo.errors import OperationFailure
from pylru import lrudecorator

log = logging.getLogger('pminifier')

class Minifier(object):
    alphabet = '2FQYNEJAUsbGu41zndZTeocMai5H7OIjXkKg8qyt3WC9hLplxfVBm0wSRr6vPD'

    class DoesNotExist(Exception):
        "The requested URL does not exist in the table."

    def __init__(self, mongo_host, mongo_db):
        if isinstance(mongo_host, basestring) or isinstance(mongo_host, list):
            self.conn = pymongo.Connection(mongo_host)
        else:
            self.conn = mongo_host
        self.db = self.conn[mongo_db]
        self._init_mongo()

    def _init_mongo(self):
        """Initialize mongo indexes and sharding."""
        # Index only when necessary. Many attempts to make the same index
        # at the same moment (from a migration for example) causes mongo
        # to throw confusing errors
        index = [('url', 1)]
        indexes = self.db.urlById.index_information()
        indexes = [i['key'] for i in indexes.values()]
        if index not in indexes:
            log.warning('Creating urlById index')
            self.db.urlById.ensure_index(index, background=False)

        # Only way to check if we're in a sharded environment
        try:
            self.db.command({'isdbgrid': 1})
        except OperationFailure as ex:
            return # nope

        db_info = self.conn.config.databases.find_one({'_id': self.db.name})
        if not db_info or not db_info['partitioned']:
            self.conn.admin.command({"enablesharding": self.db.name})

        fullname = '%s.%s' % (self.db.name, 'urlById')
        coll_info = self.conn.config.collections.find_one({'_id': fullname})
        if not coll_info or 'key' not in coll_info:
            self.conn.admin.command({'shardcollection': fullname,
                                     'key': {'_id': 1}})

    def get_id(self, url, groupkey):
        """Returns the minified ID of the url"""
        res = self._get_id_multi([url], groupkey, as_str=True)
        return res[url] if res else None

    def get_multiple_ids(self, urls, groupkey):
        """Returns the minified ID of the url"""
        return self._get_id_multi(urls, groupkey, as_str=True)

    def _get_id_multi(self, urls, groupkey, as_str=False):
        if not urls:
            return None

        entries = self.db.urlById.find({'url': {'$in':urls}}, fields=['_id', 'groupkey','url'])
        entries = [e for e in entries if e.get('groupkey') == groupkey]
        found = set([entry['url'] for entry in entries])
        notfound = set(urls) - set(found)

        res = {}
        for entry in entries:
            res[entry['url']] = self.int_to_base62(entry['_id']) if as_str else entry[0]['_id']

        if len(notfound) == 0:
            return res

        # Create new entry for keys not found
        for url in notfound:
            counter_value = self._get_current_counter_value()
            self.db.urlById.insert({'_id': counter_value,
                                    'url': url,
                                    'groupkey': groupkey}, safe=True)

            value = self.int_to_base62(counter_value) if as_str else counter_value
            res[url] = value

        return res

    def _get_current_counter_value(self):
        counter = self.db.urlByIdMeta.find_and_modify(query={'_id': 'minifier_counter'},
                                                          update={'$inc': {'value': 1}},
                                                          upsert=True, new=True)
        return counter['value']

    def get_multiple_strings(self, ids):
        """Looks up the string by its IDs (minified or integer form)"""
        converted = {}
        for id in ids:
            if isinstance(id, basestring):
                converted[self.base62_to_int(id)] = id
            else:
                converted[id] = id

        criteria = {'_id':{'$in':converted.keys()}}
        entries = self.db.urlById.find(criteria, fields=['url'])
        res = {}

        for entry in entries:
            res[converted[ entry['_id'] ]] = entry['url']

        notfound = set(converted.values()) - set(res.keys())
        for url in notfound:
            res[url] = None

        return res

    def get_string(self, id):
        """Looks up the string by its ID (minified or integer form)"""
        res = self.get_multiple_strings([id])
        if not res or not res.get(id):
                raise Minifier.DoesNotExist('The URL provided does not exist ' +
                                            'in the minification table.')
        return res[id]

    def int_to_base62(self, id):
        """Convert the int id to a user-friendly string using base62"""
        if id < 0:
            raise ValueError("Must supply a positive integer.")
        l = len(self.alphabet)
        converted = []
        while id != 0:
            id, r = divmod(id, l)
            converted.insert(0, self.alphabet[r])
        return "".join(converted) or '0'

    def base62_to_int(self, minified):
        """Convert the base62 string back to an int"""
        if set(minified) - set(self.alphabet):
            raise ValueError("Minified ID contains invalid characters '%s'" % "".join(set(minified) - set(self.alphabet)))

        s = minified[::-1]
        l = len(self.alphabet)
        output = 0
        for i, c in enumerate(s):
            output += int(self.alphabet.index(c) * math.pow(l, i))
        return int(output)

class CachedMinifier(Minifier):
    """
    Minifier that caches its operations through a user-defined decorator
    """
    def __init__(self,
                 mongo_host,
                 mongo_db,
                 cache_client,
                 cache_decorator_class,
                 lrusize=500):
        super(CachedMinifier,self).__init__(mongo_host, mongo_db)
        lrucache = lrudecorator(lrusize)
        self.cache_client = cache_client
        self.dec = cache_decorator_class(cache_client)

        self.get_multiple_strings = self._multiple_item_cache(self.get_multiple_strings,
                                                              self.get_string)
        self.get_multiple_ids = self._multiple_item_cache(self.get_multiple_ids,
                                                          self.get_id)

        self.get_string = lrucache(self.dec(self.get_string))
        self.get_id = lrucache(self.dec(self.get_id))


    def _multiple_item_cache(self, func, single_item_func):
        """
        Wraps a function that gets multiple items with caching on a per item
        basis.

        ie a decorated get_multiple_strings(ids) will:
            - check the cache per id;
            - get any items that weren't in the cache;
            - store the items retrieved in the cache;
            - return all results regardless of cache.

        This decorator assumes that the first arg is the list of keys.
        """
        def _wrapped(*args, **kw):
            keys = args[0] # grab the list of keys
            more_args = args[1:] # store the rest of the args

            # retrieve the cached items
            cached_items = self._get_from_cache(single_item_func, keys, more_args) if keys else {}

            # retrieve the uncached items
            uncached_keys = list(set(keys) - set(cached_items.keys()))
            uncached_args = [uncached_keys] + list(more_args)
            super_func = getattr(super(CachedMinifier, self), func.__name__)
            uncached_items = super_func(*uncached_args)

            # cache what wasn't cached
            if uncached_items:
                self._set_in_cache(single_item_func, uncached_items, more_args)
                cached_items.update(uncached_items)

            return cached_items
        return _wrapped


    def _get_key(self, single_item_func, item, more_args):
        if more_args:
            args = tuple([self, item] + list(more_args))
        else:
            args = (self, item,)
        key = self.dec._cache_key(single_item_func, args, {})
        return key


    def _get_from_cache(self, single_item_func, items, more_args=None):
        key_mapping = {self._get_key(single_item_func, item, more_args): item
                       for item in items}
        datas = {}
        if hasattr(self.cache_client, 'get_all'):
            data = self.cache_client.get_all(key_mapping.keys())
            datas = {k:v for k,v in zip(items, data) if v}
        else:
            for key in keys:
                data = self.cache_client.get(key)
                if data:
                    datas.setdefault(key_mapping.get(key), data)
        return datas


    def _set_in_cache(self, single_item_func, results, more_args=None):
        for item in results:
            res = results[item]
            key = self._get_key(single_item_func, item, more_args)
            self.cache_client.set(key, res)



class SimplerMinifier(Minifier):
    # mini:<group_key>:<type (id|str)>:<hash>
    key_format = "mini:{group_key}:{get_type}:{hashed}"
    cache_expiry = 60 * 60 * 24 # these don't go bad, set expire to 1d

    def __init__(self, mongo_db, redis_conn, group_key):
        self._cache_conn = redis_conn
        self.group_key = group_key
        if isinstance(mongo_db, tuple):
            connection, name = mongo_db
            super(SimplerMinifier,self).__init__(connection, name)
            self._mongo_db = self.db
        else:
            self._mongo_db = mongo_db
            super(SimplerMinifier,self).__init__(mongo_db.connection, mongo_db.name)
                

    @lrudecorator(500)
    def get_id(self, url):
        return self.get_ids([url]).get(url)

    def get_ids(self, urls):
        """returns minified for the given urls"""
        lookup_func = lambda items: self._get_id_multi(items,
                                                       self.group_key,
                                                       as_str=True)
        return self._get_items(urls, 'str', lookup_func)


    @lrudecorator(500)
    def get_string(self, minifier_id):
        res = self.get_strings([minifier_id]).get(minifier_id)
        if not res:
            raise Minifier.DoesNotExist('The URL provided does not exist.')
        return res

    def get_strings(self, minifier_ids):
        """Looks up the string by its ID (minified or integer form)"""
        def lookup_func(items):
            # decode from base62 to int
            from_int = {self.base62_to_int(item): item for item in items}
            criteria = {'_id': {'$in': from_int.keys()}}
            urls = self._mongo_db.urlById.find(criteria, fields=['url'])
            return {from_int[rec["_id"]]: rec["url"] for rec in urls}

        return self._get_items(minifier_ids, "id", lookup_func)


    def _get_items(self, items, get_type, lookup_func):
        """Looks up the string by its ID (minified or integer form)"""
        if not items:
            return {}
        keys = self._cache_key_names(get_type, items)

        # check cache
        lookup_keys = keys.keys()
        res = {key: val for key, val in zip(lookup_keys,
                                            self._cache_conn.mget(lookup_keys)) if val}

        missing_keys = set(keys.keys()) - set(res.keys())
        if missing_keys:
            missing_items = {keys[key]: key for key in missing_keys}
            found = lookup_func(missing_items.keys())

            # convert from {item: val} to {cache_key: val} so we can cache
            found = {missing_items[item]: val for item, val in found.iteritems()}
            self._store_cache(found)
            res.update(found)

        # the res now is cache_key -> minified, translate that to url -> minified
        return {keys[key]: val for key, val in res.iteritems()}

    def _cache_key_names(self, get_type, keys):
        """generates a {cache_key: key} dict for the given keys"""
        return {self.key_format.format(group_key=self.group_key,
                                       get_type=get_type,
                                       hashed=md5.md5(unicode(key)).hexdigest()): key for key in keys}

    def _store_cache(self, cache_dict):
        """saves the dict to cache with the default expiration"""
        with self._cache_conn.pipeline() as pipe:
            for cache_key, val in cache_dict.iteritems():
                # need reverse key now
                pipe.set(cache_key, val)
                pipe.expire(cache_key, self.cache_expiry)
            pipe.execute()
