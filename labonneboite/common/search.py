# coding: utf8

from datetime import datetime
import collections
import itertools
import logging
import random
import unidecode

from elasticsearch import Elasticsearch
from slugify import slugify

from labonneboite.common.models import Office
from labonneboite.conf import settings
from labonneboite.common import geocoding
from labonneboite.common import mapping as mapping_util

logger = logging.getLogger('main')

PUBLIC_ALL = 0
PUBLIC_JUNIOR = 1
PUBLIC_SENIOR = 2
PUBLIC_HANDICAP = 3
PUBLIC_CHOICES = [PUBLIC_ALL, PUBLIC_JUNIOR, PUBLIC_SENIOR, PUBLIC_HANDICAP]


class LocationError(Exception):
    pass


class JobException(Exception):
    pass


class Fetcher(object):

    def __init__(self, **kwargs):
        self.job = kwargs.get('job')
        self.city = kwargs.get('city')
        self.zipcode = kwargs.get('zipcode')
        self.occupation = kwargs.get('occupation')
        self.distance = kwargs.get('distance')
        self.naf = kwargs.get('naf')
        self.headcount = kwargs.get('headcount')
        self.sort = kwargs.get('sort')
        self.flag_alternance = kwargs.get('flag_alternance')
        public = kwargs.get('public')
        self.flag_junior = public == PUBLIC_JUNIOR
        self.flag_senior = public == PUBLIC_SENIOR
        self.flag_handicap = public == PUBLIC_HANDICAP
        self.naf_codes = []
        self.alternative_rome_codes = {}
        self.alternative_distances = collections.OrderedDict()
        self.latitude = None
        self.longitude = None
        self.rome = None
        self.from_number = int(kwargs.get('from') or 1)
        self.to_number = int(kwargs.get('to') or 10)

    def get_companies_for_rome_and_naf_codes(self, rome_codes, naf_codes, distance=None):
        if distance is None:
            distance = self.distance
        companies = _get_companies_from_api(
            rome_codes,
            naf_codes,
            self.longitude,
            self.latitude,
            distance,
            self.headcount,
            self.sort,
            self.from_number,
            self.to_number,
            self.flag_alternance,
            self.flag_junior,
            self.flag_senior,
            self.flag_handicap)
        search_companies = {}
        for company in companies:
            search_companies[company["siret"]] = company
        siret_list = [company["siret"] for company in companies]
        if siret_list:
            companies = Office.query.filter(Office.siret.in_(siret_list))
            company_by_siret = {}
            for company in companies:
                search_company = search_companies[company.siret]
                company.x = search_company["lon"]
                company.y = search_company["lat"]
                company.distance = search_company["distance"]
                company_by_siret[company.siret] = company
        self.companies = []
        for siret in siret_list:
            self.companies.append(company_by_siret[siret])
        return self.companies

    def get_company_count(self, rome_codes, naf_codes, distance):
        naf_codes = get_api_ready_rome_and_naf_codes(rome_codes, naf_codes)

        # We only fully support single-rome search.
        if len(rome_codes) > 1:
            raise Exception("multi ROME search not supported")
        rome_code = rome_codes[0]

        return count_companies_for_naf_codes(
            naf_codes,
            self.latitude,
            self.longitude,
            distance,
            flag_alternance=self.flag_alternance,
            flag_junior=self.flag_junior,
            flag_senior=self.flag_senior,
            flag_handicap=self.flag_handicap,
            headcount_filter=self.headcount,
            rome_code=rome_code)

    def get_companies(self):
        try:
            self.latitude, self.longitude = geocoding.get_lat_long_from_zipcode(self.zipcode)
            logger.info("location found for %s %s : lat=%s long=%s",
                self.city, self.zipcode, self.latitude, self.longitude)
        except:
            logger.info("location error for %s %s", self.city, self.zipcode)
            raise LocationError

        self.rome = mapping_util.SLUGIFIED_ROME_LABELS[self.occupation]
        self.company_count = self.get_company_count([self.rome], [self.naf], self.distance)
        logger.debug("set company_count to %s from get_companies", self.company_count)

        if self.from_number < 1:
            self.from_number = 1
            self.to_number = 10
        if (self.from_number - 1) % 10:
            self.from_number = 1
            self.to_number = 10
        if self.to_number > self.company_count + 1:
            self.to_number = self.company_count + 1
        if self.to_number < self.from_number:
            # this happens if a page out of bound is requested
            self.from_number = 1
            self.to_number = 10
        if self.to_number - self.from_number > settings.PAGINATION_COMPANIES_PER_PAGE:
            self.from_number = 1
            self.to_number = 10

        result = []
        if self.company_count:
            result = self.get_companies_for_rome_and_naf_codes([self.rome], [self.naf], self.distance)
        if self.company_count < 10:
            alternative_rome_codes = settings.ROME_MOBILITIES[self.rome]
            for rome in alternative_rome_codes:
                if not rome == self.rome:
                    self.naf_codes = []
                    company_count = self.get_company_count([rome], [self.naf], self.distance)
                    self.alternative_rome_codes[rome] = company_count
            last_count = 0
            for distance, distance_label in [(30, '30 km'), (50, '50 km'), (3000, u'France entière')]:
                self.naf_codes = []
                company_count = self.get_company_count([self.rome], [self.naf], distance)
                if company_count > last_count:
                    last_count = company_count
                    self.alternative_distances[distance] = (distance_label, last_count)
        return result

    def get_first_rome_suggestion(self, job):
        logger.debug("get suggestions for input %s", job)
        suggestions = build_job_label_suggestions(job)
        if not suggestions:
            raise JobException
        return [sugg['id'] for sugg in suggestions][0]


def get_api_ready_rome_and_naf_codes(rome_codes, naf_codes):
    mapper = mapping_util.Rome2NafMapper()
    for rome in rome_codes:
        if rome not in mapping_util.ROME_CODES:
            raise Exception('bad rome code %s' % rome)

    if naf_codes and not naf_codes[0]:
        naf_codes = []

    if naf_codes:
        naf_codes = mapper.map(rome_codes, naf_codes)
    else:
        naf_codes = mapper.map(rome_codes)
    return naf_codes


def _get_companies_from_api(
        rome_codes,
        naf_codes,
        longitude,
        latitude,
        distance,
        headcount,
        sort,
        from_number,
        to_number,
        flag_alternance,
        flag_junior,
        flag_senior,
        flag_handicap):
    """Internal function to be used to avoid the http overhead as for now
    the application server and the API server are on the same server.
    """
    try:
        headcount_filter = int(headcount)
    except TypeError:
        headcount_filter = settings.HEADCOUNT_WHATEVER
    except ValueError:
        headcount_filter = settings.HEADCOUNT_WHATEVER
    naf_codes = get_api_ready_rome_and_naf_codes(rome_codes, naf_codes)

    # We only fully support single-rome search.
    if len(rome_codes) > 1:
        raise Exception("multi ROME search not supported")
    rome_code = rome_codes[0]

    companies, _ = get_companies_for_naf_codes(
        naf_codes, latitude, longitude, distance, from_number, to_number,
        flag_alternance=flag_alternance,
        flag_junior=flag_junior,
        flag_senior=flag_senior,
        flag_handicap=flag_handicap,
        headcount_filter=headcount_filter, sort=sort, index=settings.ES_INDEX,
        rome_code=rome_code)
    return [company.as_json() for company in companies]


def count_companies_for_naf_codes(*args, **kwargs):
    if 'index' in kwargs:
        index = kwargs.pop('index')
    else:
        index = 'labonneboite'
    json_body = build_json_body_elastic_search(*args, **kwargs)
    del json_body["sort"]
    es = Elasticsearch()
    res = es.count(index=index, doc_type="office", body=json_body)
    return res["count"]


def get_companies_for_naf_codes(*args, **kwargs):
    if 'index' in kwargs:
        index = kwargs.pop('index')
    else:
        index = 'labonneboite'
    json_body = build_json_body_elastic_search(*args, **kwargs)
    try:
        distance_sort = kwargs['sort'] == 'distance'
    except KeyError:
        distance_sort = True
    companies, companies_count = retrieve_companies_from_elastic_search(
        json_body,
        index=index,
        distance_sort=distance_sort
        )

    try:
        rome_code = kwargs['rome_code']
    except KeyError:
        rome_code = None
    companies = shuffle_companies(companies, distance_sort, rome_code)
    return companies, companies_count


def shuffle_companies(companies, distance_sort, rome_code):
    """
    Slightly shuffle the results of a company search this way:
    1) in case of sort by score (default)
    - split results in groups of companies having the exact same stars (e.g. 2.3 or 4.9)
    - shuffle each of these groups in a predictable reproductible way
    Note that the scores are adjusted to the contextual rome_code.
    2) in case of sort by distance
    same things as 1°, grouping instead companies having the same distance in km.
    """
    buckets = collections.OrderedDict()
    for company in companies:
        if distance_sort:
            key = company.distance
        else:
            if company.score == 100:
                # make an exception for offices which were manually boosted (score 100)
                # to ensure they consistently appear on top of results
                # and are not shuffled with other offices having 5.0 stars,
                # but only score 99 and not 100
                key = 100  # special value designed to be distinct from 5.0 stars
            else:
                key = company.get_stars_for_rome_code(rome_code)
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(company)

    # generating now predictable yet divergent seed for shuffle
    # the list of results should be noticeably different from one day to the other,
    # but stay the same for a given day
    day_of_year = datetime.now().timetuple().tm_yday
    shuffle_seed = day_of_year / 366.0

    for _, bucket in buckets.iteritems():
        random.shuffle(bucket, lambda: shuffle_seed)
    companies = list(itertools.chain.from_iterable(buckets.values()))

    return companies


def build_json_body_elastic_search(
        naf_codes, latitude, longitude, distance,
        from_number=None, to_number=None, headcount_filter=settings.HEADCOUNT_WHATEVER,
        sort="distance", flag_alternance=0, flag_junior=0, flag_senior=0, flag_handicap=0,
        rome_code=None):

    sort_attrs = []

    naf_filter = {
        "terms": {
            "naf": naf_codes
        }
    }

    filters = [naf_filter]

    # in some cases, a string is given as input, let's ensure it is an int from now on
    try:
        headcount_filter = int(headcount_filter)
    except ValueError:
        headcount_filter = settings.HEADCOUNT_WHATEVER

    min_office_size = None
    max_office_size = None
    if headcount_filter == settings.HEADCOUNT_SMALL_ONLY:
        max_office_size = settings.HEADCOUNT_SMALL_ONLY_MAXIMUM
    elif headcount_filter == settings.HEADCOUNT_BIG_ONLY:
        min_office_size = settings.HEADCOUNT_BIG_ONLY_MINIMUM

    if min_office_size or max_office_size:
        if min_office_size:
            headcount_filter = {"gte": min_office_size}
        if max_office_size:
            headcount_filter = {"lte": max_office_size}
        headcount_filter_dic = {
            "numeric_range": {
                "headcount": headcount_filter
            }
        }
        filters += [headcount_filter_dic]

    if flag_alternance == 1:
        flag_alternance_filter = {
            "term": {
                "flag_alternance": 1
            }
        }
        filters += [flag_alternance_filter]

    if flag_junior == 1:
        flag_junior_filter = {
            "term": {
                "flag_junior": 1
            }
        }
        filters += [flag_junior_filter]

    if flag_senior == 1:
        flag_senior_filter = {
            "term": {
                "flag_senior": 1
            }
        }
        filters += [flag_senior_filter]

    if flag_handicap == 1:
        flag_handicap_filter = {
            "term": {
                "flag_handicap": 1
            }
        }
        filters += [flag_handicap_filter]

    if sort not in ['distance', 'score']:
        logger.info('sort should be distance or score: %s', sort)
        sort = 'distance'

    distance_sort = {
        "_geo_distance": {
            "locations": {
                "lat": latitude,
                "lon": longitude
            },
            "order": "asc",
            "unit": "km"
        }
    }

    if rome_code is None:
        score_sort = {
            "score": {
                "order": "desc"
            }
        }
    else:
        field_name = "score_for_rome_%s" % rome_code
        score_sort = {
            field_name: {
                "order": "desc"
            }
        }
        score_for_rome_filter = {
            "exists": {
                "field": field_name
            }
        }
        filters.append(score_for_rome_filter)

    if sort == "distance":
        sort_attrs.append(distance_sort)
        sort_attrs.append(score_sort)
    elif sort == "score":
        sort_attrs.append(score_sort)
        sort_attrs.append(distance_sort)

    filters.append({
        "geo_distance": {
            "distance": "%skm" % distance,
            "locations": {
                "lat": latitude,
                "lon": longitude
            }
        }
    })

    json_body = {
        "sort": sort_attrs,
        "query": {
            "filtered": {
                "filter": {
                    "bool": {
                        "must": filters
                    }
                }
            }
        }
    }

    if from_number:
        json_body["from"] = from_number - 1
        if to_number:
            if to_number < from_number:
                # this should never happen
                logger.exception("to_number < from_number : %s < %s", to_number, from_number)
                raise Exception("to_number < from_number")
            json_body["size"] = to_number - from_number + 1
    return json_body


def retrieve_companies_from_elastic_search(json_body, distance_sort=True, index="labonneboite"):
    es = Elasticsearch()
    res = es.search(index=index, doc_type="office", body=json_body)
    logger.info("Elastic Search request : %s", json_body)
    companies = []
    siret_list = []
    distances = {}
    if distance_sort:
        distance_sort_index = 0
    else:
        distance_sort_index = 1

    for office in res['hits']['hits']:
        siret = office["_source"]["siret"]
        siret_list.append(siret)
        distances[siret] = int(round(office["sort"][distance_sort_index]))

    if siret_list:
        company_objects = Office.query.filter(Office.siret.in_(siret_list))
        company_dict = {}

        for obj in company_objects:
            obj.distance = distances[obj.siret]
            company_dict[obj.siret] = obj

        for siret in siret_list:
            company = company_dict[siret]
            if company.has_city():
                companies.append(company)
            else:
                logging.info("company siret %s does not have city, ignoring...", siret)

    companies_count = res['hits']['total']
    return companies, companies_count


def build_location_suggestions(term):
    term = term.title()
    es = Elasticsearch()
    zipcode_match = [{
        "prefix": {
            "zipcode": term
        }
    }, ]

    city_match = [{
        "match": {
            "city_name.autocomplete": {
                "query": term
            }
        }}, {
        "match": {
            "city_name.stemmed": {
                "query": term,
                "boost": 1
            }
        }}, {
        "match_phrase_prefix": {
            "city_name.stemmed": {
                "query": term
            }
        }}]

    filters = zipcode_match

    try:
        int(term)
    except ValueError:
        filters.extend(city_match)

    body = {
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "should": filters
                    },
                },
                "field_value_factor": {
                    "field": "population",
                    "modifier": "log1p"
                }
            },
        },
        "size": 10
    }
    res = es.search(index="labonneboite", doc_type="location", body=body)

    suggestions = []
    first_score = None

    for hit in res['hits']['hits']:
        if not first_score:
            first_score = hit['_score']
        source = hit['_source']
        if source['zipcode']:  # and hit['_score'] > 0.1 * first_score:
            city_name = source['city_name'].replace('"', '')
            label = u'%s (%s)' % (city_name, source['zipcode'])
            city = {
                'city': city_name.lower(),
                'zipcode': source['zipcode'],
                'label': label,
                'latitude': source['location']['lat'],
                'longitude': source['location']['lon']
            }
            suggestions.append(city)
    return suggestions


def build_job_label_suggestions(term):

    es = Elasticsearch()

    body = {
        "_source": ["ogr_description", "rome_description", "rome_code"],
        "query": {
            "match": {
                # Query for multiple words or multiple parts of words across multiple fields.
                # Based on https://qbox.io/blog/an-introduction-to-ngrams-in-elasticsearch
                "_all": unidecode.unidecode(term)
            }
        },
        "aggs":{
            "by_rome_code": {
                "terms": {
                    "field": "rome_code",
                    "size": 0,
                    # Note: a maximum of 550 buckets will be fetched, as we have 550 unique ROME codes

                    # TOFIX: `order` cannot work without a computed `max_score`, see the `max_score` comment below.
                    # Order results by sub-aggregation named 'max_score'
                    # "order": {"max_score": "desc"},
                },
                "aggs": {
                    # Only 1 result per rome code: include only 1 top hit on each bucket in the results.
                    "by_top_hit": {"top_hits": {"size": 1}},

                    # TOFIX: `max_score` below does not work with Elasticsearch 1.7.
                    # Fixed in elasticsearch 2.0+:
                    # https://github.com/elastic/elasticsearch/issues/10091#issuecomment-193676966

                    # Count of the max score of any member of this bucket
                    # "max_score": {"max": {"lang": "expression", "script": "_score"}},
                },
            },
        },
        "size": 0,
    }

    res = es.search(index="labonneboite", doc_type="ogr", body=body)

    suggestions = []

    # Since ordering cannot be done easily through Elasticsearch 1.7 (`max_score` not working),
    # we do it in Python at this time.
    results = res[u'aggregations'][u'by_rome_code'][u'buckets']
    results.sort(key=lambda e: e['by_top_hit']['hits']['max_score'], reverse=True)

    for hit in results:
        if len(suggestions) < settings.AUTOCOMPLETE_MAX:
            hit = hit[u'by_top_hit'][u'hits'][u'hits'][0]
            source = hit['_source']
            highlight = hit.get('highlight', {})
            try:
                rome_description = highlight['rome_description.autocomplete'][0]
            except KeyError:
                rome_description = source['rome_description']
            try:
                ogr_description = highlight['ogr_description.autocomplete'][0]
            except KeyError:
                ogr_description = source['ogr_description']
            label = "%s (%s, ...)" % (rome_description, ogr_description)
            value = "%s (%s, ...)" % (source["rome_description"], source["ogr_description"])
            suggestions.append({
                'id': source['rome_code'],
                'label': label,
                'value': value,
                'occupation': slugify(source['rome_description'].lower()),
            })
        else:
            break

    return suggestions
