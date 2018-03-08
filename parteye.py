#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2018 Paco Esteban <paco@onna.be>
#
# Distributed under terms of the MIT license.

import base64
import collections
import configparser
import hashlib
import hmac
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from subprocess import run

import requests
from requests.auth import HTTPBasicAuth

config = configparser.ConfigParser()
config.read('config.ini')


def tme_api_call(action, params):
    """calls TME API and returns the json response

    it handles all the hmac signature and all that ...
    :action: string action url after the main TME api domain
    :params: dict with params for the request
    """

    api_url = 'https://api.tme.eu/' + action + '.json'
    params['Token'] = config["tme"]["token"]

    # params need to be ordered
    params = collections.OrderedDict(sorted(params.items()))
    encoded_params = urllib.parse.urlencode(params, '')
    signature_base = ('POST' + '&' + urllib.parse.quote(api_url, '') + '&' +
                      urllib.parse.quote(encoded_params, ''))

    api_signature = base64.encodestring(
        hmac.new(
            bytes(config["tme"]["secret"], 'UTF-8'),
            bytes(signature_base, 'UTF-8'), hashlib.sha1).digest()).rstrip()
    params['ApiSignature'] = api_signature

    try:
        r = requests.post(api_url, params)
        r.raise_for_status()
    except requests.exceptions.HTTPError as err:
        print(err)
        sys.exit(1)

    return r.json()


def pk_api_call(method, url, **kwargs):
    """calls Partkeepr API

    :method: requst method
    :url: part of the url to call (without base)
    :data: tata to pass to the request if any
    :returns: requests object

    """
    pk_user = config["partkeepr"]["user"]
    pk_pwd = config["partkeepr"]["pwd"]
    pk_url = config["partkeepr"]["url"]
    try:
        r = requests.request(
            method,
            pk_url + url,
            **kwargs,
            auth=HTTPBasicAuth(pk_user, pk_pwd),
        )
        r.raise_for_status()
    except requests.exceptions.HTTPError as err:
        print(err)
        sys.exit(1)

    return r


def read_in():
    """
    Reads a line from stdin and returns it
    """
    return sys.stdin.readline().strip()


def parse_tme(raw_in):
    """
    Parses the raw data comming from stdin (barcode reader)
    :raw_in: string in the form:
    QTY:1 PN:HA50151V4 MFR:SUNON MPN:HA50151V4-000U-999
    PO:5094268/9 https://www.tme.eu/details/HA50151V4
    Where :
    FIELD   NAME   Desc
    0       QTY    Quantity
    1       PN     Part Number
    2       MFR    Manufacturer
    3       MPN    Manufacturer part number
    4       PO     Order Number (at TME)
    5       URL    Url of the product at vendor(TME)
    """
    part = {
        'PN': raw_in[1].split(":")[1],
        'Quantity': raw_in[0].split(":")[1],
        'Files': [],
        'Case': '',
        'PO': raw_in[4].split(":")[1]
    }

    params = {'SymbolList[0]': part["PN"], 'Country': 'ES', 'Language': 'EN'}

    run(["/usr/bin/play", "-q", "./beep.wav"])
    print("Looking for part: {}".format(part["PN"]))

    # first we get the description of the part
    product = tme_api_call('Products/GetProducts', params)
    part["Desc"] = product["Data"]["ProductList"][0]["Description"]

    # then we get the footprint name if any
    parameters = tme_api_call('Products/GetParameters', params)
    symbols = parameters["Data"]["ProductList"][0]["ParameterList"]
    for param in symbols:
        if param["ParameterId"] == "35" or param["ParameterId"] == "2932":
            part["Case"] = param["ParameterValue"]

    # finally we get all pdfs related to this part
    files = tme_api_call('Products/GetProductsFiles', params)
    docs = files["Data"]["ProductList"][0]["Files"]["DocumentList"]
    for d in docs:
        if d["DocumentUrl"][-3:] == "pdf":
            part["Files"].append("https:" + d["DocumentUrl"])

    part["Files"] = list(set(part["Files"]))

    return part


def generate_footprint(fp):
    """Checks for footprint if it exists

    :fp: string footprint name
    :returns: json structure to attach to part creation or None
    """
    if len(fp) == 0:
        return None

    params = {
        'filter':
        '{{"property":"name","operator":"=","value":"{}"}}'.format(fp)
    }
    r = pk_api_call('get', '/api/footprints', params=params)

    rj = r.json()
    if len(rj["hydra:member"]) > 0:
        return rj["hydra:member"][0]

    return None


def upload_attachments(files):
    """Check if there's any file for the part and uploads it to partkeepr

    :files: files arry
    :returns: json structure to attach to part creation or None
    """
    uploads = []
    if len(files) > 0:
        for f in files:
            r = pk_api_call(
                'post', '/api/temp_uploaded_files/upload', data={'url': f})
            uploads.append(r.json()["response"])

        return uploads

    return None


def insert_part(part):
    """ Inserts the part in partkeepr.
    If found, it just adds stock

    :part: dict representing the part
    """
    # we first look for the part name
    params = {
        'filter':
        '{{"property":"name","operator":"=","value":"{}"}}'.format(part["PN"])
    }
    r = pk_api_call('get', '/api/parts', params=params)

    rj = r.json()
    # if found, just add stock and return
    if len(rj["hydra:member"]) > 0:
        r = pk_api_call(
            'put',
            '{}/addStock'.format(rj["hydra:member"][0]["@id"]),
            data={
                'quantity': part["Quantity"],
                'comment': part["PO"]
            })
        print("{} - Increased stock in {} units".format(
            part["PN"], part["Quantity"]))
        return

    # if not, we prepare the json payload for insert
    with open('request.json') as json_data:
        d = json.load(json_data)

    date = datetime.now()

    d["name"] = part["PN"]
    d["description"] = part["Desc"]
    d["stockLevels"][0]["stockLevel"] = part["Quantity"]
    d["createDate"] = date.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    d["footprint"] = generate_footprint(part["Case"])
    # here files are uploaded to tmp
    d["attachments"] = upload_attachments(part["Files"])

    r = pk_api_call('post', '/api/parts', json=d)
    r.raise_for_status()

    print("Part {} ({} new units) loaded to Partkeepr".format(
        part["PN"], part["Quantity"]))


while True:
    line = read_in()  # read from stdin

    r = re.compile(r'^QTY:\d+ PN:.*tme\.eu.*')
    if r.match(line) is not None:  # is this from TME ?
        my_part = parse_tme(line.split(" "))
    else:
        if not line:
            print("bye !")
            sys.exit(0)
        print("Unrecognized raw data format")
        sys.exit(1)

    insert_part(my_part)
