import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.prometheus import get_cross_node_status


@pytest.mark.parametrize('cidr,instance', [
    ('10.42.2.0/24', '10.42.2.5:8000'),
    ('10.42.9.0/24', '10.42.9.8:8000'),
    ('fd00:42:2::/64', '[fd00:42:2::5]:8000'),
])
@pytest.mark.parametrize('value,expected', [('1', 'up'), ('0', 'down')])
def test_worker_scrapes_follow_current_cidr(cidr, instance, value, expected):
    nodes = [
        {'roles': ['control-plane'], 'pod_cidrs': ['10.42.0.0/24']},
        {'roles': ['worker'], 'pod_cidrs': [cidr]},
    ]
    result = [
        {'metric': {'instance': instance}, 'value': [0, value]},
        # Stale WSL, control-plane, and host-network targets must not affect health.
        *[{'metric': {'instance': ip}, 'value': [0, '0']} for ip in
          ['10.42.1.5:8000', '10.42.0.5:8000', '192.168.1.63:9100']],
    ]
    response = httpx.Response(200, json={'data': {'result': result}}, request=httpx.Request('GET', 'http://prometheus'))
    client = AsyncMock()
    client.get.return_value = response
    assert asyncio.run(get_cross_node_status(client, nodes)) == expected


def test_no_worker_targets_is_unknown():
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json={'data': {'result': []}}, request=httpx.Request('GET', 'http://prometheus'))
    assert asyncio.run(get_cross_node_status(client, [{'roles': ['worker'], 'pod_cidrs': ['10.42.2.0/24']}])) is None


def test_missing_node_data_is_unknown():
    client = AsyncMock()
    assert asyncio.run(get_cross_node_status(client, [])) is None
    client.get.assert_not_called()


def test_prometheus_failure_is_unknown():
    client = AsyncMock()
    client.get.side_effect = OSError('unreachable')
    assert asyncio.run(get_cross_node_status(client, [{'roles': ['worker'], 'pod_cidrs': ['10.42.2.0/24']}])) is None
