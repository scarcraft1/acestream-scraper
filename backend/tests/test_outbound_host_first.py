"""The pinned client sends Host first, where an ordinary client puts it.

Measured 15-10: appended after requests' default headers, Dailymotion's CDN
answered 403 to a request a plain requests.get got 200 for.
"""
import requests

from app.utils.outbound_http import PinnedHTTPAdapter


def test_HOST_VA_LA_PRIMERA():
    prepared = requests.Request('GET', 'https://cdn.example.com/x.m3u8',
                                headers={'User-Agent': 'UA', 'Sec-Fetch-Mode': 'cors'}).prepare()
    PinnedHTTPAdapter().add_headers(prepared)
    names = list(prepared.headers.keys())
    assert names[0] == 'Host' and prepared.headers['Host'] == 'cdn.example.com'
    assert names[1:] == ['User-Agent', 'Sec-Fetch-Mode']


def test_UN_HOST_QUE_YA_VENIA_NO_SE_DUPLICA():
    prepared = requests.Request('GET', 'https://cdn.example.com:8443/x', headers={'host': 'other', 'A': '1'}).prepare()
    PinnedHTTPAdapter().add_headers(prepared)
    assert [k.lower() for k in prepared.headers.keys()] == ['host', 'a']
    assert prepared.headers['Host'] == 'cdn.example.com:8443'
