import unittest
import unittest.mock
import textwrap
import asyncio

from aioredis import ReplyError, ProtocolError, RedisPool
from aioredis.cluster import RedisCluster, RedisPoolCluster
from aioredis.cluster.cluster import (
    parse_moved_response_error, parse_nodes_info, ClusterNodesManager,
    ClusterNode
)
from aioredis.errors import RedisClusterError
from ._testutil import (
    SLOT_ZERO_KEY, run_until_complete, BaseTest, cluster_test,
    CreateConnectionMock, FakeConnection, PoolConnectionMock
)


# example from the CLUSTER NODES doc
RAW_NODE_INFO_DATA_OK = textwrap.dedent("""
    07c37dfeb235213a872192d90877d0cd55635b91 127.0.0.1:30004 slave \
    e7d1eecce10fd6bb5eb35b9f99a514335d9ba9ca 0 1426238317239 4 connected
    67ed2db8d677e59ec4a4cefb06858cf2a1a89fa1 127.0.0.1:30002 master \
    - 0 1426238316232 2 connected 5461-10922
    292f8b365bb7edb5e285caf0b7e6ddc7265d2f4f 127.0.0.1:30003 master \
    - 0 1426238318243 3 connected 10923-16383
    6ec23923021cf3ffec47632106199cb7f496ce01 127.0.0.1:30005 slave \
    67ed2db8d677e59ec4a4cefb06858cf2a1a89fa1 0 1426238316232 5 connected
    824fe116063bc5fcf9f4ffd895bc17aee7731ac3 127.0.0.1:30006 slave \
    292f8b365bb7edb5e285caf0b7e6ddc7265d2f4f 0 1426238317741 6 connected
    e7d1eecce10fd6bb5eb35b9f99a514335d9ba9ca 127.0.0.1:30001 myself,master \
    - 0 0 1 connected 0-5460
""")

RAW_NODE_INFO_DATA_FAIL = textwrap.dedent("""
    07c37dfeb235213a872192d90877d0cd55635b91 127.0.0.1:30004 slave,fail \
    e7d1eecce10fd6bb5eb35b9f99a514335d9ba9ca 0 1426238317239 4 connected
    67ed2db8d677e59ec4a4cefb06858cf2a1a89fa1 127.0.0.1:30002 master,fail? \
    - 0 1426238316232 2 connected 5461-10922
    292f8b365bb7edb5e285caf0b7e6ddc7265d2f4f 127.0.0.1:30003 master \
    - 0 1426238318243 3 connected 10923-16383
    6ec23923021cf3ffec47632106199cb7f496ce01 127.0.0.1:30005 slave \
    67ed2db8d677e59ec4a4cefb06858cf2a1a89fa1 0 1426238316232 5 connected
    824fe116063bc5fcf9f4ffd895bc17aee7731ac3 127.0.0.1:30006 slave \
    292f8b365bb7edb5e285caf0b7e6ddc7265d2f4f 0 1426238317741 6 connected
    e7d1eecce10fd6bb5eb35b9f99a514335d9ba9ca 127.0.0.1:30001 myself,master \
    - 0 0 1 connected 0-5460
""")


class ParseTest(unittest.TestCase):
    def test_parse_moved_response_error(self):
        self.assertIsNone(parse_moved_response_error(ReplyError()))
        self.assertIsNone(parse_moved_response_error(ReplyError('ASK')))
        self.assertEqual(
            parse_moved_response_error(
                ReplyError('MOVED 3999 127.0.0.1:6381')),
            ('127.0.0.1', 6381)
        )

    def test_parse_nodes_info(self):
        self.assertTupleEqual(
            list(parse_nodes_info(RAW_NODE_INFO_DATA_FAIL,
                                  ClusterNodesManager.CLUSTER_NODES_TUPLE))[0],
            [
                ('07c37dfeb235213a872192d90877d0cd55635b91',
                 '127.0.0.1', 30004, ('slave', 'fail'),
                 'e7d1eecce10fd6bb5eb35b9f99a514335d9ba9ca',
                 'connected', ((0, 0), )
                 ),
                ('67ed2db8d677e59ec4a4cefb06858cf2a1a89fa1',
                 '127.0.0.1', 30002, ('master', 'fail?'),
                 '0', 'connected', ((5461, 10922), )
                 ),
                ('292f8b365bb7edb5e285caf0b7e6ddc7265d2f4f',
                 '127.0.0.1', 30003, ('master', ), '0',
                 'connected', ((10923, 16383), )
                 ),
                ('6ec23923021cf3ffec47632106199cb7f496ce01',
                 '127.0.0.1', 30005, ('slave', ),
                 '67ed2db8d677e59ec4a4cefb06858cf2a1a89fa1',
                 'connected', ((0, 0), )
                 ),
                ('824fe116063bc5fcf9f4ffd895bc17aee7731ac3',
                 '127.0.0.1', 30006, ('slave', ),
                 '292f8b365bb7edb5e285caf0b7e6ddc7265d2f4f',
                 'connected', ((0, 0), )
                 ),
                ('e7d1eecce10fd6bb5eb35b9f99a514335d9ba9ca',
                 '127.0.0.1', 30001, ('myself', 'master'),
                 '0', 'connected', ((0, 5460), )
                 ),
            ][0]
        )


class ClusterNodesManagerTest(unittest.TestCase):
    def test_key_slot(self):
        self.assertEqual(ClusterNodesManager.key_slot(SLOT_ZERO_KEY), 0)
        self.assertEqual(ClusterNodesManager.key_slot('key'), 12539)
        self.assertEqual(ClusterNodesManager.key_slot(b'key'), 12539)

    def test_create(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_FAIL)
        self.assertEqual(len(manager.nodes), 6)
        self.assertTrue(all(isinstance(node, ClusterNode)
                            for node in manager.nodes))

    def test_node_count(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_FAIL)
        self.assertEqual(manager.nodes_count, 4)
        self.assertEqual(manager.masters_count, 2)
        self.assertEqual(manager.slaves_count, 2)

    def test_alive_nodes(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_FAIL)
        self.assertEqual(manager.alive_nodes, manager.nodes[2:])

    def test_cluster_node(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_FAIL)
        node1 = manager.nodes[0]
        self.assertFalse(node1.is_master)
        self.assertTrue(node1.is_slave)
        self.assertEqual(node1.address, ('127.0.0.1', 30004))
        self.assertFalse(node1.is_alive)

        node2 = manager.nodes[2]
        self.assertTrue(node2.is_master)
        self.assertFalse(node2.is_slave)
        self.assertTrue(node2.is_alive)

    def test_in_range(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_FAIL)
        master = manager.nodes[5]
        self.assertTrue(master.in_range(0))
        self.assertTrue(master.in_range(5460))
        self.assertFalse(master.in_range(5461))

    def test_all_slots_covered(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_OK)
        self.assertTrue(manager.all_slots_covered)

        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_FAIL)
        self.assertFalse(manager.all_slots_covered)

        modified_data = RAW_NODE_INFO_DATA_OK.replace('16383', '16382')
        manager = ClusterNodesManager.create(modified_data)
        self.assertFalse(manager.all_slots_covered)

    def test_determine_slot(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_OK)
        self.assertEqual(manager.determine_slot('key'), 12539)

    def test_determine_slot_multiple(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_OK)
        self.assertEqual(manager.determine_slot('{key}:1', '{key}:2'), 12539)

    def test_determine_slot_multiple_different(self):
        manager = ClusterNodesManager.create(RAW_NODE_INFO_DATA_OK)
        with self.assertRaises(RedisClusterError):
            manager.determine_slot('key:1', 'key:2')


class RedisClusterTest(BaseTest):
    @cluster_test()
    @run_until_complete
    def test_create(self):
        cluster = yield from self.create_test_cluster()
        self.assertIsInstance(cluster, RedisCluster)

    @cluster_test()
    @run_until_complete
    def test_create_fails(self):
        expected_connections = {
            port: FakeConnection(
                self, port, return_value=ProtocolError('Intentional error'))
            for port in range(self.redis_port, self.redis_port + 6)
        }
        with CreateConnectionMock(self, expected_connections):
            with self.assertRaises(RedisClusterError):
                yield from self.create_test_cluster()

    @cluster_test()
    @run_until_complete
    def test_counts(self):
        cluster = yield from self.create_test_cluster()
        self.assertEqual(cluster.node_count(), 6)
        self.assertEqual(cluster.masters_count(), 3)
        self.assertEqual(cluster.slave_count(), 3)

    @cluster_test()
    @run_until_complete
    def test_get_node(self):
        cluster = yield from self.create_test_cluster()
        # Compare script used to setup the test cluster
        node = cluster.get_node('GET', 'key:0')
        self.assertEqual(node.address[1], self.redis_port)
        node = cluster.get_node('GET', b'key:1')
        self.assertEqual(node.address[1], self.redis_port + 1)
        node = cluster.get_node('GET', b'key:3', 'more', 'args')
        self.assertEqual(node.address[1], self.redis_port + 2)

    @cluster_test()
    @run_until_complete
    def test_get_node_eval(self):
        cluster = yield from self.create_test_cluster()

        node = cluster.get_node(
            'EVAL', keys=['{key}:1', '{key}:2'], args=['more', 'args'])
        self.assertEqual(node.address[1], self.redis_port + 2)

        with self.assertRaises(RedisClusterError):
            cluster.get_node('EVAL', keys=['keys', 'in', 'different', 'slots'])

    @cluster_test()
    @run_until_complete
    def test_execute(self):
        cluster = yield from self.create_test_cluster()
        expected_connection = FakeConnection(self, self.redis_port)
        with CreateConnectionMock(
                self, {self.redis_port: expected_connection}):
            ok = yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        self.assertTrue(ok)
        expected_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_with_moved(self):
        cluster = yield from self.create_test_cluster()
        expected_connections = {
            self.redis_port: FakeConnection(
                self, self.redis_port,
                return_value=ReplyError('MOVED 6000 127.0.0.1:{}'
                                        .format(self.redis_port + 1))
            ),
            self.redis_port + 1: FakeConnection(self, self.redis_port + 1)
        }
        with CreateConnectionMock(self, expected_connections):
            ok = yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        self.assertTrue(ok)
        expected_connections[self.redis_port].execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')
        expected_connections[self.redis_port + 1].execute\
            .assert_called_once_with(b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_with_reply_error(self):
        cluster = yield from self.create_test_cluster()
        expected_connection = FakeConnection(
            self, self.redis_port, return_value=ReplyError('ERROR'))
        with CreateConnectionMock(self,
                                  {self.redis_port: expected_connection}):
            with self.assertRaises(ReplyError):
                yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        expected_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_with_protocol_error(self):
        cluster = yield from self.create_test_cluster()
        expected_connection = FakeConnection(
            self, self.redis_port, return_value=ProtocolError('ERROR'))
        with CreateConnectionMock(self,
                                  {self.redis_port: expected_connection}):
            with self.assertRaises(ProtocolError):
                yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        expected_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_many(self):
        cluster = yield from self.create_test_cluster()
        expected_connections = {
            port: FakeConnection(self, port)
            for port in range(self.redis_port, self.redis_port + 3)
        }

        with CreateConnectionMock(self, expected_connections):
            ok = yield from cluster.execute('PING')

        self.assertEqual(ok, [b'OK'] * 3)
        for connection in expected_connections.values():
            connection.execute.assert_called_once_with(
                'PING', encoding=unittest.mock.ANY)


class RedisPoolClusterTest(BaseTest):
    @asyncio.coroutine
    def create_test_pool_cluster(self, **kwargs):
        nodes = self.get_cluster_addresses(self.redis_port)
        return self.create_pool_cluster(nodes, loop=self.loop, **kwargs)

    @cluster_test()
    @run_until_complete
    def test_create(self):
        coro = self.create_test_pool_cluster()
        cluster = yield from coro
        self.assertIsInstance(cluster, RedisPoolCluster)

    @cluster_test()
    @run_until_complete
    def test_get_pool(self):
        cluster = yield from self.create_test_pool_cluster()
        # Compare the redis_trib.rb script used to setup the test cluster
        pool = cluster.get_pool('GET', 'key:0')
        self.assertEqual(pool._address[1], self.redis_port)
        pool = cluster.get_pool('GET', b'key:1')
        self.assertEqual(pool._address[1], self.redis_port + 1)
        pool = cluster.get_pool('GET', b'key:3', 'more', 'args')
        self.assertEqual(pool._address[1], self.redis_port + 2)

    @cluster_test()
    @run_until_complete
    def test_cluster_misconfigured(self):
        with self.assertRaises(RedisClusterError):
            yield from self.create_test_pool_cluster(password="1234")

    @cluster_test()
    @run_until_complete
    def test_get_cluster_pool_fails(self):
        cluster = yield from self.create_test_pool_cluster()
        with unittest.mock.patch('aioredis.cluster.cluster.create_pool') \
                as create_pool:
            pool_futures = [asyncio.Future(loop=self.loop) for i in range(3)]
            mock_pool = unittest.mock.Mock(spec=RedisPool)
            mock_pool.clear.return_value = asyncio.Future(loop=self.loop)
            mock_pool.clear.return_value.set_result(None)
            pool_futures[0].set_result(mock_pool)
            pool_futures[2].set_exception(RuntimeError())
            create_pool.side_effect = pool_futures
            with self.assertRaises(RuntimeError):
                yield from cluster.get_cluster_pool()
            mock_pool.clear.assert_called_once_with(close=True)
            self.assertTrue(pool_futures[1].cancelled())

    @cluster_test()
    @run_until_complete
    def test_execute(self):
        cluster = yield from self.create_test_pool_cluster()
        expected_connection = FakeConnection(self, self.redis_port)
        with PoolConnectionMock(self, cluster,
                                {self.redis_port: expected_connection}):
            ok = yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        self.assertTrue(ok)
        expected_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_with_moved(self):
        cluster = yield from self.create_test_pool_cluster()
        expected_pool_connection = FakeConnection(
            self, self.redis_port,
            return_value=ReplyError('MOVED 6000 127.0.0.1:{}'
                                    .format(self.redis_port + 1))
        )
        expected_direct_connection = FakeConnection(self, self.redis_port + 1)

        with PoolConnectionMock(
                self, cluster, {self.redis_port: expected_pool_connection}):
            with CreateConnectionMock(
                    self, {self.redis_port + 1: expected_direct_connection}):
                ok = yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        self.assertTrue(ok)
        expected_pool_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')
        expected_direct_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_with_reply_error(self):
        cluster = yield from self.create_test_pool_cluster()
        expected_connection = FakeConnection(
            self, self.redis_port, return_value=ReplyError('ERROR'))
        with PoolConnectionMock(
                self, cluster, {self.redis_port: expected_connection}):
            with self.assertRaises(ReplyError):
                yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        expected_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_with_protocol_error(self):
        cluster = yield from self.create_test_pool_cluster()
        expected_connection = FakeConnection(
            self, self.redis_port, return_value=ProtocolError('ERROR'))
        with PoolConnectionMock(
                self, cluster, {self.redis_port: expected_connection}):
            with self.assertRaises(ProtocolError):
                yield from cluster.execute('SET', SLOT_ZERO_KEY, 'value')

        expected_connection.execute.assert_called_once_with(
            b'SET', SLOT_ZERO_KEY, 'value')

    @cluster_test()
    @run_until_complete
    def test_execute_many(self):
        cluster = yield from self.create_test_pool_cluster()
        expected_connections = {
            port: FakeConnection(self, port)
            for port in range(self.redis_port, self.redis_port + 3)
        }

        with PoolConnectionMock(self, cluster, expected_connections):
            ok = yield from cluster.execute('PING')

        self.assertEqual(ok, [b'OK'] * 3)
        for connection in expected_connections.values():
            connection.execute.assert_called_once_with(
                'PING', encoding=unittest.mock.ANY)

    @cluster_test()
    @run_until_complete
    def test_reload_cluster_pool(self):
        cluster = yield from self.create_test_pool_cluster()
        old_pools = list(cluster._cluster_pool.values())
        yield from cluster.reload_cluster_pool()
        new_pools = list(cluster._cluster_pool.values())
        self.assertTrue(len(new_pools) > 0)
        self.assertTrue({id(pool) for pool in old_pools}
                        .isdisjoint({id(pool) for pool in new_pools}))

    @cluster_test()
    @run_until_complete
    def test_clear_cluster_pool_fails(self):
        cluster = yield from self.create_test_pool_cluster()
        pools = cluster._cluster_pool.values()
        with unittest.mock.patch('aioredis.pool.RedisPool.clear') \
                as pool_clear:
            result = asyncio.Future(loop=self.loop)
            result.set_result(None)
            pool_clear.side_effect = [result, RuntimeError(), result]
            with self.assertRaises(RuntimeError):
                yield from cluster.clear()
            self.assertEqual(pool_clear.call_count, 3)

        # Really close pools to avoid pending task destroyed errors
        for pool in pools:
            yield from pool.clear(close=True)
