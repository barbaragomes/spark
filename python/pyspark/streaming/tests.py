#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import glob
import os
import sys
from itertools import chain
import time
import operator
import tempfile
import random
import struct
from functools import reduce

if sys.version_info[:2] <= (2, 6):
    try:
        import unittest2 as unittest
    except ImportError:
        sys.stderr.write('Please install unittest2 to test with Python 2.6 or earlier')
        sys.exit(1)
else:
    import unittest

from pyspark.context import SparkConf, SparkContext, RDD
from pyspark.storagelevel import StorageLevel
from pyspark.streaming.context import StreamingContext
from pyspark.streaming.kafka import Broker, KafkaUtils, OffsetRange, TopicAndPartition
from pyspark.streaming.flume import FlumeUtils
from pyspark.streaming.mqtt import MQTTUtils
from pyspark.streaming.kinesis import KinesisUtils, InitialPositionInStream


class PySparkStreamingTestCase(unittest.TestCase):

    timeout = 10  # seconds
    duration = .5

    @classmethod
    def setUpClass(cls):
        class_name = cls.__name__
        conf = SparkConf().set("spark.default.parallelism", 1)
        cls.sc = SparkContext(appName=class_name, conf=conf)
        cls.sc.setCheckpointDir("/tmp")

    @classmethod
    def tearDownClass(cls):
        cls.sc.stop()

    def setUp(self):
        self.ssc = StreamingContext(self.sc, self.duration)

    def tearDown(self):
        self.ssc.stop(False)

    def wait_for(self, result, n):
        start_time = time.time()
        while len(result) < n and time.time() - start_time < self.timeout:
            time.sleep(0.01)
        if len(result) < n:
            print("timeout after", self.timeout)

    def _take(self, dstream, n):
        """
        Return the first `n` elements in the stream (will start and stop).
        """
        results = []

        def take(_, rdd):
            if rdd and len(results) < n:
                results.extend(rdd.take(n - len(results)))

        dstream.foreachRDD(take)

        self.ssc.start()
        self.wait_for(results, n)
        return results

    def _collect(self, dstream, n, block=True):
        """
        Collect each RDDs into the returned list.

        :return: list, which will have the collected items.
        """
        result = []

        def get_output(_, rdd):
            if rdd and len(result) < n:
                r = rdd.collect()
                if r:
                    result.append(r)

        dstream.foreachRDD(get_output)

        if not block:
            return result

        self.ssc.start()
        self.wait_for(result, n)
        return result

    def _test_func(self, input, func, expected, sort=False, input2=None):
        """
        @param input: dataset for the test. This should be list of lists.
        @param func: wrapped function. This function should return PythonDStream object.
        @param expected: expected output for this testcase.
        """
        if not isinstance(input[0], RDD):
            input = [self.sc.parallelize(d, 1) for d in input]
        input_stream = self.ssc.queueStream(input)
        if input2 and not isinstance(input2[0], RDD):
            input2 = [self.sc.parallelize(d, 1) for d in input2]
        input_stream2 = self.ssc.queueStream(input2) if input2 is not None else None

        # Apply test function to stream.
        if input2:
            stream = func(input_stream, input_stream2)
        else:
            stream = func(input_stream)

        result = self._collect(stream, len(expected))
        if sort:
            self._sort_result_based_on_key(result)
            self._sort_result_based_on_key(expected)
        self.assertEqual(expected, result)

    def _sort_result_based_on_key(self, outputs):
        """Sort the list based on first value."""
        for output in outputs:
            output.sort(key=lambda x: x[0])


class BasicOperationTests(PySparkStreamingTestCase):

    def test_map(self):
        """Basic operation test for DStream.map."""
        input = [range(1, 5), range(5, 9), range(9, 13)]

        def func(dstream):
            return dstream.map(str)
        expected = [list(map(str, x)) for x in input]
        self._test_func(input, func, expected)

    def test_flatMap(self):
        """Basic operation test for DStream.faltMap."""
        input = [range(1, 5), range(5, 9), range(9, 13)]

        def func(dstream):
            return dstream.flatMap(lambda x: (x, x * 2))
        expected = [list(chain.from_iterable((map(lambda y: [y, y * 2], x))))
                    for x in input]
        self._test_func(input, func, expected)

    def test_filter(self):
        """Basic operation test for DStream.filter."""
        input = [range(1, 5), range(5, 9), range(9, 13)]

        def func(dstream):
            return dstream.filter(lambda x: x % 2 == 0)
        expected = [[y for y in x if y % 2 == 0] for x in input]
        self._test_func(input, func, expected)

    def test_count(self):
        """Basic operation test for DStream.count."""
        input = [range(5), range(10), range(20)]

        def func(dstream):
            return dstream.count()
        expected = [[len(x)] for x in input]
        self._test_func(input, func, expected)

    def test_reduce(self):
        """Basic operation test for DStream.reduce."""
        input = [range(1, 5), range(5, 9), range(9, 13)]

        def func(dstream):
            return dstream.reduce(operator.add)
        expected = [[reduce(operator.add, x)] for x in input]
        self._test_func(input, func, expected)

    def test_reduceByKey(self):
        """Basic operation test for DStream.reduceByKey."""
        input = [[("a", 1), ("a", 1), ("b", 1), ("b", 1)],
                 [("", 1), ("", 1), ("", 1), ("", 1)],
                 [(1, 1), (1, 1), (2, 1), (2, 1), (3, 1)]]

        def func(dstream):
            return dstream.reduceByKey(operator.add)
        expected = [[("a", 2), ("b", 2)], [("", 4)], [(1, 2), (2, 2), (3, 1)]]
        self._test_func(input, func, expected, sort=True)

    def test_mapValues(self):
        """Basic operation test for DStream.mapValues."""
        input = [[("a", 2), ("b", 2), ("c", 1), ("d", 1)],
                 [(0, 4), (1, 1), (2, 2), (3, 3)],
                 [(1, 1), (2, 1), (3, 1), (4, 1)]]

        def func(dstream):
            return dstream.mapValues(lambda x: x + 10)
        expected = [[("a", 12), ("b", 12), ("c", 11), ("d", 11)],
                    [(0, 14), (1, 11), (2, 12), (3, 13)],
                    [(1, 11), (2, 11), (3, 11), (4, 11)]]
        self._test_func(input, func, expected, sort=True)

    def test_flatMapValues(self):
        """Basic operation test for DStream.flatMapValues."""
        input = [[("a", 2), ("b", 2), ("c", 1), ("d", 1)],
                 [(0, 4), (1, 1), (2, 1), (3, 1)],
                 [(1, 1), (2, 1), (3, 1), (4, 1)]]

        def func(dstream):
            return dstream.flatMapValues(lambda x: (x, x + 10))
        expected = [[("a", 2), ("a", 12), ("b", 2), ("b", 12),
                     ("c", 1), ("c", 11), ("d", 1), ("d", 11)],
                    [(0, 4), (0, 14), (1, 1), (1, 11), (2, 1), (2, 11), (3, 1), (3, 11)],
                    [(1, 1), (1, 11), (2, 1), (2, 11), (3, 1), (3, 11), (4, 1), (4, 11)]]
        self._test_func(input, func, expected)

    def test_glom(self):
        """Basic operation test for DStream.glom."""
        input = [range(1, 5), range(5, 9), range(9, 13)]
        rdds = [self.sc.parallelize(r, 2) for r in input]

        def func(dstream):
            return dstream.glom()
        expected = [[[1, 2], [3, 4]], [[5, 6], [7, 8]], [[9, 10], [11, 12]]]
        self._test_func(rdds, func, expected)

    def test_mapPartitions(self):
        """Basic operation test for DStream.mapPartitions."""
        input = [range(1, 5), range(5, 9), range(9, 13)]
        rdds = [self.sc.parallelize(r, 2) for r in input]

        def func(dstream):
            def f(iterator):
                yield sum(iterator)
            return dstream.mapPartitions(f)
        expected = [[3, 7], [11, 15], [19, 23]]
        self._test_func(rdds, func, expected)

    def test_countByValue(self):
        """Basic operation test for DStream.countByValue."""
        input = [list(range(1, 5)) * 2, list(range(5, 7)) + list(range(5, 9)), ["a", "a", "b", ""]]

        def func(dstream):
            return dstream.countByValue()
        expected = [[4], [4], [3]]
        self._test_func(input, func, expected)

    def test_groupByKey(self):
        """Basic operation test for DStream.groupByKey."""
        input = [[(1, 1), (2, 1), (3, 1), (4, 1)],
                 [(1, 1), (1, 1), (1, 1), (2, 1), (2, 1), (3, 1)],
                 [("a", 1), ("a", 1), ("b", 1), ("", 1), ("", 1), ("", 1)]]

        def func(dstream):
            return dstream.groupByKey().mapValues(list)

        expected = [[(1, [1]), (2, [1]), (3, [1]), (4, [1])],
                    [(1, [1, 1, 1]), (2, [1, 1]), (3, [1])],
                    [("a", [1, 1]), ("b", [1]), ("", [1, 1, 1])]]
        self._test_func(input, func, expected, sort=True)

    def test_combineByKey(self):
        """Basic operation test for DStream.combineByKey."""
        input = [[(1, 1), (2, 1), (3, 1), (4, 1)],
                 [(1, 1), (1, 1), (1, 1), (2, 1), (2, 1), (3, 1)],
                 [("a", 1), ("a", 1), ("b", 1), ("", 1), ("", 1), ("", 1)]]

        def func(dstream):
            def add(a, b):
                return a + str(b)
            return dstream.combineByKey(str, add, add)
        expected = [[(1, "1"), (2, "1"), (3, "1"), (4, "1")],
                    [(1, "111"), (2, "11"), (3, "1")],
                    [("a", "11"), ("b", "1"), ("", "111")]]
        self._test_func(input, func, expected, sort=True)

    def test_repartition(self):
        input = [range(1, 5), range(5, 9)]
        rdds = [self.sc.parallelize(r, 2) for r in input]

        def func(dstream):
            return dstream.repartition(1).glom()
        expected = [[[1, 2, 3, 4]], [[5, 6, 7, 8]]]
        self._test_func(rdds, func, expected)

    def test_union(self):
        input1 = [range(3), range(5), range(6)]
        input2 = [range(3, 6), range(5, 6)]

        def func(d1, d2):
            return d1.union(d2)

        expected = [list(range(6)), list(range(6)), list(range(6))]
        self._test_func(input1, func, expected, input2=input2)

    def test_cogroup(self):
        input = [[(1, 1), (2, 1), (3, 1)],
                 [(1, 1), (1, 1), (1, 1), (2, 1)],
                 [("a", 1), ("a", 1), ("b", 1), ("", 1), ("", 1)]]
        input2 = [[(1, 2)],
                  [(4, 1)],
                  [("a", 1), ("a", 1), ("b", 1), ("", 1), ("", 2)]]

        def func(d1, d2):
            return d1.cogroup(d2).mapValues(lambda vs: tuple(map(list, vs)))

        expected = [[(1, ([1], [2])), (2, ([1], [])), (3, ([1], []))],
                    [(1, ([1, 1, 1], [])), (2, ([1], [])), (4, ([], [1]))],
                    [("a", ([1, 1], [1, 1])), ("b", ([1], [1])), ("", ([1, 1], [1, 2]))]]
        self._test_func(input, func, expected, sort=True, input2=input2)

    def test_join(self):
        input = [[('a', 1), ('b', 2)]]
        input2 = [[('b', 3), ('c', 4)]]

        def func(a, b):
            return a.join(b)

        expected = [[('b', (2, 3))]]
        self._test_func(input, func, expected, True, input2)

    def test_left_outer_join(self):
        input = [[('a', 1), ('b', 2)]]
        input2 = [[('b', 3), ('c', 4)]]

        def func(a, b):
            return a.leftOuterJoin(b)

        expected = [[('a', (1, None)), ('b', (2, 3))]]
        self._test_func(input, func, expected, True, input2)

    def test_right_outer_join(self):
        input = [[('a', 1), ('b', 2)]]
        input2 = [[('b', 3), ('c', 4)]]

        def func(a, b):
            return a.rightOuterJoin(b)

        expected = [[('b', (2, 3)), ('c', (None, 4))]]
        self._test_func(input, func, expected, True, input2)

    def test_full_outer_join(self):
        input = [[('a', 1), ('b', 2)]]
        input2 = [[('b', 3), ('c', 4)]]

        def func(a, b):
            return a.fullOuterJoin(b)

        expected = [[('a', (1, None)), ('b', (2, 3)), ('c', (None, 4))]]
        self._test_func(input, func, expected, True, input2)

    def test_update_state_by_key(self):

        def updater(vs, s):
            if not s:
                s = []
            s.extend(vs)
            return s

        input = [[('k', i)] for i in range(5)]

        def func(dstream):
            return dstream.updateStateByKey(updater)

        expected = [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1, 2, 3, 4]]
        expected = [[('k', v)] for v in expected]
        self._test_func(input, func, expected)


class WindowFunctionTests(PySparkStreamingTestCase):

    timeout = 15

    def test_window(self):
        input = [range(1), range(2), range(3), range(4), range(5)]

        def func(dstream):
            return dstream.window(1.5, .5).count()

        expected = [[1], [3], [6], [9], [12], [9], [5]]
        self._test_func(input, func, expected)

    def test_count_by_window(self):
        input = [range(1), range(2), range(3), range(4), range(5)]

        def func(dstream):
            return dstream.countByWindow(1.5, .5)

        expected = [[1], [3], [6], [9], [12], [9], [5]]
        self._test_func(input, func, expected)

    def test_count_by_window_large(self):
        input = [range(1), range(2), range(3), range(4), range(5), range(6)]

        def func(dstream):
            return dstream.countByWindow(2.5, .5)

        expected = [[1], [3], [6], [10], [15], [20], [18], [15], [11], [6]]
        self._test_func(input, func, expected)

    def test_count_by_value_and_window(self):
        input = [range(1), range(2), range(3), range(4), range(5), range(6)]

        def func(dstream):
            return dstream.countByValueAndWindow(2.5, .5)

        expected = [[1], [2], [3], [4], [5], [6], [6], [6], [6], [6]]
        self._test_func(input, func, expected)

    def test_group_by_key_and_window(self):
        input = [[('a', i)] for i in range(5)]

        def func(dstream):
            return dstream.groupByKeyAndWindow(1.5, .5).mapValues(list)

        expected = [[('a', [0])], [('a', [0, 1])], [('a', [0, 1, 2])], [('a', [1, 2, 3])],
                    [('a', [2, 3, 4])], [('a', [3, 4])], [('a', [4])]]
        self._test_func(input, func, expected)

    def test_reduce_by_invalid_window(self):
        input1 = [range(3), range(5), range(1), range(6)]
        d1 = self.ssc.queueStream(input1)
        self.assertRaises(ValueError, lambda: d1.reduceByKeyAndWindow(None, None, 0.1, 0.1))
        self.assertRaises(ValueError, lambda: d1.reduceByKeyAndWindow(None, None, 1, 0.1))


class StreamingContextTests(PySparkStreamingTestCase):

    duration = 0.1

    def _add_input_stream(self):
        inputs = [range(1, x) for x in range(101)]
        stream = self.ssc.queueStream(inputs)
        self._collect(stream, 1, block=False)

    def test_stop_only_streaming_context(self):
        self._add_input_stream()
        self.ssc.start()
        self.ssc.stop(False)
        self.assertEqual(len(self.sc.parallelize(range(5), 5).glom().collect()), 5)

    def test_stop_multiple_times(self):
        self._add_input_stream()
        self.ssc.start()
        self.ssc.stop(False)
        self.ssc.stop(False)

    def test_queue_stream(self):
        input = [list(range(i + 1)) for i in range(3)]
        dstream = self.ssc.queueStream(input)
        result = self._collect(dstream, 3)
        self.assertEqual(input, result)

    def test_text_file_stream(self):
        d = tempfile.mkdtemp()
        self.ssc = StreamingContext(self.sc, self.duration)
        dstream2 = self.ssc.textFileStream(d).map(int)
        result = self._collect(dstream2, 2, block=False)
        self.ssc.start()
        for name in ('a', 'b'):
            time.sleep(1)
            with open(os.path.join(d, name), "w") as f:
                f.writelines(["%d\n" % i for i in range(10)])
        self.wait_for(result, 2)
        self.assertEqual([list(range(10)), list(range(10))], result)

    def test_binary_records_stream(self):
        d = tempfile.mkdtemp()
        self.ssc = StreamingContext(self.sc, self.duration)
        dstream = self.ssc.binaryRecordsStream(d, 10).map(
            lambda v: struct.unpack("10b", bytes(v)))
        result = self._collect(dstream, 2, block=False)
        self.ssc.start()
        for name in ('a', 'b'):
            time.sleep(1)
            with open(os.path.join(d, name), "wb") as f:
                f.write(bytearray(range(10)))
        self.wait_for(result, 2)
        self.assertEqual([list(range(10)), list(range(10))], [list(v[0]) for v in result])

    def test_union(self):
        input = [list(range(i + 1)) for i in range(3)]
        dstream = self.ssc.queueStream(input)
        dstream2 = self.ssc.queueStream(input)
        dstream3 = self.ssc.union(dstream, dstream2)
        result = self._collect(dstream3, 3)
        expected = [i * 2 for i in input]
        self.assertEqual(expected, result)

    def test_transform(self):
        dstream1 = self.ssc.queueStream([[1]])
        dstream2 = self.ssc.queueStream([[2]])
        dstream3 = self.ssc.queueStream([[3]])

        def func(rdds):
            rdd1, rdd2, rdd3 = rdds
            return rdd2.union(rdd3).union(rdd1)

        dstream = self.ssc.transform([dstream1, dstream2, dstream3], func)

        self.assertEqual([2, 3, 1], self._take(dstream, 3))


class CheckpointTests(unittest.TestCase):

    def test_get_or_create(self):
        inputd = tempfile.mkdtemp()
        outputd = tempfile.mkdtemp() + "/"

        def updater(vs, s):
            return sum(vs, s or 0)

        def setup():
            conf = SparkConf().set("spark.default.parallelism", 1)
            sc = SparkContext(conf=conf)
            ssc = StreamingContext(sc, 0.5)
            dstream = ssc.textFileStream(inputd).map(lambda x: (x, 1))
            wc = dstream.updateStateByKey(updater)
            wc.map(lambda x: "%s,%d" % x).saveAsTextFiles(outputd + "test")
            wc.checkpoint(.5)
            return ssc

        cpd = tempfile.mkdtemp("test_streaming_cps")
        ssc = StreamingContext.getOrCreate(cpd, setup)
        ssc.start()

        def check_output(n):
            while not os.listdir(outputd):
                time.sleep(0.01)
            time.sleep(1)  # make sure mtime is larger than the previous one
            with open(os.path.join(inputd, str(n)), 'w') as f:
                f.writelines(["%d\n" % i for i in range(10)])

            while True:
                p = os.path.join(outputd, max(os.listdir(outputd)))
                if '_SUCCESS' not in os.listdir(p):
                    # not finished
                    time.sleep(0.01)
                    continue
                ordd = ssc.sparkContext.textFile(p).map(lambda line: line.split(","))
                d = ordd.values().map(int).collect()
                if not d:
                    time.sleep(0.01)
                    continue
                self.assertEqual(10, len(d))
                s = set(d)
                self.assertEqual(1, len(s))
                m = s.pop()
                if n > m:
                    continue
                self.assertEqual(n, m)
                break

        check_output(1)
        check_output(2)
        ssc.stop(True, True)

        time.sleep(1)
        ssc = StreamingContext.getOrCreate(cpd, setup)
        ssc.start()
        check_output(3)
        ssc.stop(True, True)


class KafkaStreamTests(PySparkStreamingTestCase):
    timeout = 20  # seconds
    duration = 1

    def setUp(self):
        super(KafkaStreamTests, self).setUp()

        kafkaTestUtilsClz = self.ssc._jvm.java.lang.Thread.currentThread().getContextClassLoader()\
            .loadClass("org.apache.spark.streaming.kafka.KafkaTestUtils")
        self._kafkaTestUtils = kafkaTestUtilsClz.newInstance()
        self._kafkaTestUtils.setup()

    def tearDown(self):
        if self._kafkaTestUtils is not None:
            self._kafkaTestUtils.teardown()
            self._kafkaTestUtils = None

        super(KafkaStreamTests, self).tearDown()

    def _randomTopic(self):
        return "topic-%d" % random.randint(0, 10000)

    def _validateStreamResult(self, sendData, stream):
        result = {}
        for i in chain.from_iterable(self._collect(stream.map(lambda x: x[1]),
                                                   sum(sendData.values()))):
            result[i] = result.get(i, 0) + 1

        self.assertEqual(sendData, result)

    def _validateRddResult(self, sendData, rdd):
        result = {}
        for i in rdd.map(lambda x: x[1]).collect():
            result[i] = result.get(i, 0) + 1
        self.assertEqual(sendData, result)

    def test_kafka_stream(self):
        """Test the Python Kafka stream API."""
        topic = self._randomTopic()
        sendData = {"a": 3, "b": 5, "c": 10}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)

        stream = KafkaUtils.createStream(self.ssc, self._kafkaTestUtils.zkAddress(),
                                         "test-streaming-consumer", {topic: 1},
                                         {"auto.offset.reset": "smallest"})
        self._validateStreamResult(sendData, stream)

    def test_kafka_direct_stream(self):
        """Test the Python direct Kafka stream API."""
        topic = self._randomTopic()
        sendData = {"a": 1, "b": 2, "c": 3}
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress(),
                       "auto.offset.reset": "smallest"}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)

        stream = KafkaUtils.createDirectStream(self.ssc, [topic], kafkaParams)
        self._validateStreamResult(sendData, stream)

    @unittest.skipIf(sys.version >= "3", "long type not support")
    def test_kafka_direct_stream_from_offset(self):
        """Test the Python direct Kafka stream API with start offset specified."""
        topic = self._randomTopic()
        sendData = {"a": 1, "b": 2, "c": 3}
        fromOffsets = {TopicAndPartition(topic, 0): long(0)}
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress()}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)

        stream = KafkaUtils.createDirectStream(self.ssc, [topic], kafkaParams, fromOffsets)
        self._validateStreamResult(sendData, stream)

    @unittest.skipIf(sys.version >= "3", "long type not support")
    def test_kafka_rdd(self):
        """Test the Python direct Kafka RDD API."""
        topic = self._randomTopic()
        sendData = {"a": 1, "b": 2}
        offsetRanges = [OffsetRange(topic, 0, long(0), long(sum(sendData.values())))]
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress()}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)
        rdd = KafkaUtils.createRDD(self.sc, kafkaParams, offsetRanges)
        self._validateRddResult(sendData, rdd)

    @unittest.skipIf(sys.version >= "3", "long type not support")
    def test_kafka_rdd_with_leaders(self):
        """Test the Python direct Kafka RDD API with leaders."""
        topic = self._randomTopic()
        sendData = {"a": 1, "b": 2, "c": 3}
        offsetRanges = [OffsetRange(topic, 0, long(0), long(sum(sendData.values())))]
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress()}
        address = self._kafkaTestUtils.brokerAddress().split(":")
        leaders = {TopicAndPartition(topic, 0): Broker(address[0], int(address[1]))}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)
        rdd = KafkaUtils.createRDD(self.sc, kafkaParams, offsetRanges, leaders)
        self._validateRddResult(sendData, rdd)

    @unittest.skipIf(sys.version >= "3", "long type not support")
    def test_kafka_rdd_get_offsetRanges(self):
        """Test Python direct Kafka RDD get OffsetRanges."""
        topic = self._randomTopic()
        sendData = {"a": 3, "b": 4, "c": 5}
        offsetRanges = [OffsetRange(topic, 0, long(0), long(sum(sendData.values())))]
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress()}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)
        rdd = KafkaUtils.createRDD(self.sc, kafkaParams, offsetRanges)
        self.assertEqual(offsetRanges, rdd.offsetRanges())

    @unittest.skipIf(sys.version >= "3", "long type not support")
    def test_kafka_direct_stream_foreach_get_offsetRanges(self):
        """Test the Python direct Kafka stream foreachRDD get offsetRanges."""
        topic = self._randomTopic()
        sendData = {"a": 1, "b": 2, "c": 3}
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress(),
                       "auto.offset.reset": "smallest"}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)

        stream = KafkaUtils.createDirectStream(self.ssc, [topic], kafkaParams)

        offsetRanges = []

        def getOffsetRanges(_, rdd):
            for o in rdd.offsetRanges():
                offsetRanges.append(o)

        stream.foreachRDD(getOffsetRanges)
        self.ssc.start()
        self.wait_for(offsetRanges, 1)

        self.assertEqual(offsetRanges, [OffsetRange(topic, 0, long(0), long(6))])

    @unittest.skipIf(sys.version >= "3", "long type not support")
    def test_kafka_direct_stream_transform_get_offsetRanges(self):
        """Test the Python direct Kafka stream transform get offsetRanges."""
        topic = self._randomTopic()
        sendData = {"a": 1, "b": 2, "c": 3}
        kafkaParams = {"metadata.broker.list": self._kafkaTestUtils.brokerAddress(),
                       "auto.offset.reset": "smallest"}

        self._kafkaTestUtils.createTopic(topic)
        self._kafkaTestUtils.sendMessages(topic, sendData)

        stream = KafkaUtils.createDirectStream(self.ssc, [topic], kafkaParams)

        offsetRanges = []

        def transformWithOffsetRanges(rdd):
            for o in rdd.offsetRanges():
                offsetRanges.append(o)
            return rdd

        stream.transform(transformWithOffsetRanges).foreachRDD(lambda rdd: rdd.count())
        self.ssc.start()
        self.wait_for(offsetRanges, 1)

        self.assertEqual(offsetRanges, [OffsetRange(topic, 0, long(0), long(6))])


class FlumeStreamTests(PySparkStreamingTestCase):
    timeout = 20  # seconds
    duration = 1

    def setUp(self):
        super(FlumeStreamTests, self).setUp()

        utilsClz = self.ssc._jvm.java.lang.Thread.currentThread().getContextClassLoader() \
            .loadClass("org.apache.spark.streaming.flume.FlumeTestUtils")
        self._utils = utilsClz.newInstance()

    def tearDown(self):
        if self._utils is not None:
            self._utils.close()
            self._utils = None

        super(FlumeStreamTests, self).tearDown()

    def _startContext(self, n, compressed):
        # Start the StreamingContext and also collect the result
        dstream = FlumeUtils.createStream(self.ssc, "localhost", self._utils.getTestPort(),
                                          enableDecompression=compressed)
        result = []

        def get_output(_, rdd):
            for event in rdd.collect():
                if len(result) < n:
                    result.append(event)
        dstream.foreachRDD(get_output)
        self.ssc.start()
        return result

    def _validateResult(self, input, result):
        # Validate both the header and the body
        header = {"test": "header"}
        self.assertEqual(len(input), len(result))
        for i in range(0, len(input)):
            self.assertEqual(header, result[i][0])
            self.assertEqual(input[i], result[i][1])

    def _writeInput(self, input, compressed):
        # Try to write input to the receiver until success or timeout
        start_time = time.time()
        while True:
            try:
                self._utils.writeInput(input, compressed)
                break
            except:
                if time.time() - start_time < self.timeout:
                    time.sleep(0.01)
                else:
                    raise

    def test_flume_stream(self):
        input = [str(i) for i in range(1, 101)]
        result = self._startContext(len(input), False)
        self._writeInput(input, False)
        self.wait_for(result, len(input))
        self._validateResult(input, result)

    def test_compressed_flume_stream(self):
        input = [str(i) for i in range(1, 101)]
        result = self._startContext(len(input), True)
        self._writeInput(input, True)
        self.wait_for(result, len(input))
        self._validateResult(input, result)


class FlumePollingStreamTests(PySparkStreamingTestCase):
    timeout = 20  # seconds
    duration = 1
    maxAttempts = 5

    def setUp(self):
        utilsClz = \
            self.sc._jvm.java.lang.Thread.currentThread().getContextClassLoader() \
                .loadClass("org.apache.spark.streaming.flume.PollingFlumeTestUtils")
        self._utils = utilsClz.newInstance()

    def tearDown(self):
        if self._utils is not None:
            self._utils.close()
            self._utils = None

    def _writeAndVerify(self, ports):
        # Set up the streaming context and input streams
        ssc = StreamingContext(self.sc, self.duration)
        try:
            addresses = [("localhost", port) for port in ports]
            dstream = FlumeUtils.createPollingStream(
                ssc,
                addresses,
                maxBatchSize=self._utils.eventsPerBatch(),
                parallelism=5)
            outputBuffer = []

            def get_output(_, rdd):
                for e in rdd.collect():
                    outputBuffer.append(e)

            dstream.foreachRDD(get_output)
            ssc.start()
            self._utils.sendDatAndEnsureAllDataHasBeenReceived()

            self.wait_for(outputBuffer, self._utils.getTotalEvents())
            outputHeaders = [event[0] for event in outputBuffer]
            outputBodies = [event[1] for event in outputBuffer]
            self._utils.assertOutput(outputHeaders, outputBodies)
        finally:
            ssc.stop(False)

    def _testMultipleTimes(self, f):
        attempt = 0
        while True:
            try:
                f()
                break
            except:
                attempt += 1
                if attempt >= self.maxAttempts:
                    raise
                else:
                    import traceback
                    traceback.print_exc()

    def _testFlumePolling(self):
        try:
            port = self._utils.startSingleSink()
            self._writeAndVerify([port])
            self._utils.assertChannelsAreEmpty()
        finally:
            self._utils.close()

    def _testFlumePollingMultipleHosts(self):
        try:
            port = self._utils.startSingleSink()
            self._writeAndVerify([port])
            self._utils.assertChannelsAreEmpty()
        finally:
            self._utils.close()

    def test_flume_polling(self):
        self._testMultipleTimes(self._testFlumePolling)

    def test_flume_polling_multiple_hosts(self):
        self._testMultipleTimes(self._testFlumePollingMultipleHosts)


class MQTTStreamTests(PySparkStreamingTestCase):
    timeout = 20  # seconds
    duration = 1

    def setUp(self):
        super(MQTTStreamTests, self).setUp()

        MQTTTestUtilsClz = self.ssc._jvm.java.lang.Thread.currentThread().getContextClassLoader() \
            .loadClass("org.apache.spark.streaming.mqtt.MQTTTestUtils")
        self._MQTTTestUtils = MQTTTestUtilsClz.newInstance()
        self._MQTTTestUtils.setup()

    def tearDown(self):
        if self._MQTTTestUtils is not None:
            self._MQTTTestUtils.teardown()
            self._MQTTTestUtils = None

        super(MQTTStreamTests, self).tearDown()

    def _randomTopic(self):
        return "topic-%d" % random.randint(0, 10000)

    def _startContext(self, topic):
        # Start the StreamingContext and also collect the result
        stream = MQTTUtils.createStream(self.ssc, "tcp://" + self._MQTTTestUtils.brokerUri(), topic)
        result = []

        def getOutput(_, rdd):
            for data in rdd.collect():
                result.append(data)

        stream.foreachRDD(getOutput)
        self.ssc.start()
        return result

    def test_mqtt_stream(self):
        """Test the Python MQTT stream API."""
        sendData = "MQTT demo for spark streaming"
        topic = self._randomTopic()
        result = self._startContext(topic)

        def retry():
            self._MQTTTestUtils.publishData(topic, sendData)
            # Because "publishData" sends duplicate messages, here we should use > 0
            self.assertTrue(len(result) > 0)
            self.assertEqual(sendData, result[0])

        # Retry it because we don't know when the receiver will start.
        self._retry_or_timeout(retry)

    def _retry_or_timeout(self, test_func):
        start_time = time.time()
        while True:
            try:
                test_func()
                break
            except:
                if time.time() - start_time > self.timeout:
                    raise
                time.sleep(0.01)


class KinesisStreamTests(PySparkStreamingTestCase):

    def test_kinesis_stream_api(self):
        # Don't start the StreamingContext because we cannot test it in Jenkins
        kinesisStream1 = KinesisUtils.createStream(
            self.ssc, "myAppNam", "mySparkStream",
            "https://kinesis.us-west-2.amazonaws.com", "us-west-2",
            InitialPositionInStream.LATEST, 2, StorageLevel.MEMORY_AND_DISK_2)
        kinesisStream2 = KinesisUtils.createStream(
            self.ssc, "myAppNam", "mySparkStream",
            "https://kinesis.us-west-2.amazonaws.com", "us-west-2",
            InitialPositionInStream.LATEST, 2, StorageLevel.MEMORY_AND_DISK_2,
            "awsAccessKey", "awsSecretKey")

    def test_kinesis_stream(self):
        if not are_kinesis_tests_enabled:
            sys.stderr.write(
                "Skipped test_kinesis_stream (enable by setting environment variable %s=1"
                % kinesis_test_environ_var)
            return

        import random
        kinesisAppName = ("KinesisStreamTests-%d" % abs(random.randint(0, 10000000)))
        kinesisTestUtilsClz = \
            self.sc._jvm.java.lang.Thread.currentThread().getContextClassLoader() \
                .loadClass("org.apache.spark.streaming.kinesis.KinesisTestUtils")
        kinesisTestUtils = kinesisTestUtilsClz.newInstance()
        try:
            kinesisTestUtils.createStream()
            aWSCredentials = kinesisTestUtils.getAWSCredentials()
            stream = KinesisUtils.createStream(
                self.ssc, kinesisAppName, kinesisTestUtils.streamName(),
                kinesisTestUtils.endpointUrl(), kinesisTestUtils.regionName(),
                InitialPositionInStream.LATEST, 10, StorageLevel.MEMORY_ONLY,
                aWSCredentials.getAWSAccessKeyId(), aWSCredentials.getAWSSecretKey())

            outputBuffer = []

            def get_output(_, rdd):
                for e in rdd.collect():
                    outputBuffer.append(e)

            stream.foreachRDD(get_output)
            self.ssc.start()

            testData = [i for i in range(1, 11)]
            expectedOutput = set([str(i) for i in testData])
            start_time = time.time()
            while time.time() - start_time < 120:
                kinesisTestUtils.pushData(testData)
                if expectedOutput == set(outputBuffer):
                    break
                time.sleep(10)
            self.assertEqual(expectedOutput, set(outputBuffer))
        except:
            import traceback
            traceback.print_exc()
            raise
        finally:
            self.ssc.stop(False)
            kinesisTestUtils.deleteStream()
            kinesisTestUtils.deleteDynamoDBTable(kinesisAppName)


def search_kafka_assembly_jar():
    SPARK_HOME = os.environ["SPARK_HOME"]
    kafka_assembly_dir = os.path.join(SPARK_HOME, "external/kafka-assembly")
    jars = glob.glob(
        os.path.join(kafka_assembly_dir, "target/scala-*/spark-streaming-kafka-assembly-*.jar"))
    if not jars:
        raise Exception(
            ("Failed to find Spark Streaming kafka assembly jar in %s. " % kafka_assembly_dir) +
            "You need to build Spark with "
            "'build/sbt assembly/assembly streaming-kafka-assembly/assembly' or "
            "'build/mvn package' before running this test.")
    elif len(jars) > 1:
        raise Exception(("Found multiple Spark Streaming Kafka assembly JARs in %s; please "
                         "remove all but one") % kafka_assembly_dir)
    else:
        return jars[0]


def search_flume_assembly_jar():
    SPARK_HOME = os.environ["SPARK_HOME"]
    flume_assembly_dir = os.path.join(SPARK_HOME, "external/flume-assembly")
    jars = glob.glob(
        os.path.join(flume_assembly_dir, "target/scala-*/spark-streaming-flume-assembly-*.jar"))
    if not jars:
        raise Exception(
            ("Failed to find Spark Streaming Flume assembly jar in %s. " % flume_assembly_dir) +
            "You need to build Spark with "
            "'build/sbt assembly/assembly streaming-flume-assembly/assembly' or "
            "'build/mvn package' before running this test.")
    elif len(jars) > 1:
        raise Exception(("Found multiple Spark Streaming Flume assembly JARs in %s; please "
                        "remove all but one") % flume_assembly_dir)
    else:
        return jars[0]


def search_mqtt_assembly_jar():
    SPARK_HOME = os.environ["SPARK_HOME"]
    mqtt_assembly_dir = os.path.join(SPARK_HOME, "external/mqtt-assembly")
    jars = glob.glob(
        os.path.join(mqtt_assembly_dir, "target/scala-*/spark-streaming-mqtt-assembly-*.jar"))
    if not jars:
        raise Exception(
            ("Failed to find Spark Streaming MQTT assembly jar in %s. " % mqtt_assembly_dir) +
            "You need to build Spark with "
            "'build/sbt assembly/assembly streaming-mqtt-assembly/assembly' or "
            "'build/mvn package' before running this test")
    elif len(jars) > 1:
        raise Exception(("Found multiple Spark Streaming MQTT assembly JARs in %s; please "
                         "remove all but one") % mqtt_assembly_dir)
    else:
        return jars[0]


def search_mqtt_test_jar():
    SPARK_HOME = os.environ["SPARK_HOME"]
    mqtt_test_dir = os.path.join(SPARK_HOME, "external/mqtt")
    jars = glob.glob(
        os.path.join(mqtt_test_dir, "target/scala-*/spark-streaming-mqtt-test-*.jar"))
    if not jars:
        raise Exception(
            ("Failed to find Spark Streaming MQTT test jar in %s. " % mqtt_test_dir) +
            "You need to build Spark with "
            "'build/sbt assembly/assembly streaming-mqtt/test:assembly'")
    elif len(jars) > 1:
        raise Exception(("Found multiple Spark Streaming MQTT test JARs in %s; please "
                         "remove all but one") % mqtt_test_dir)
    else:
        return jars[0]


def search_kinesis_asl_assembly_jar():
    SPARK_HOME = os.environ["SPARK_HOME"]
    kinesis_asl_assembly_dir = os.path.join(SPARK_HOME, "extras/kinesis-asl-assembly")
    jars = glob.glob(
        os.path.join(kinesis_asl_assembly_dir,
                     "target/scala-*/spark-streaming-kinesis-asl-assembly-*.jar"))
    if not jars:
        return None
    elif len(jars) > 1:
        raise Exception(("Found multiple Spark Streaming Kinesis ASL assembly JARs in %s; please "
                         "remove all but one") % kinesis_asl_assembly_dir)
    else:
        return jars[0]


# Must be same as the variable and condition defined in KinesisTestUtils.scala
kinesis_test_environ_var = "ENABLE_KINESIS_TESTS"
are_kinesis_tests_enabled = os.environ.get(kinesis_test_environ_var) == '1'

if __name__ == "__main__":
    kafka_assembly_jar = search_kafka_assembly_jar()
    flume_assembly_jar = search_flume_assembly_jar()
    mqtt_assembly_jar = search_mqtt_assembly_jar()
    mqtt_test_jar = search_mqtt_test_jar()
    kinesis_asl_assembly_jar = search_kinesis_asl_assembly_jar()

    if kinesis_asl_assembly_jar is None:
        kinesis_jar_present = False
        jars = "%s,%s,%s,%s" % (kafka_assembly_jar, flume_assembly_jar, mqtt_assembly_jar,
                                mqtt_test_jar)
    else:
        kinesis_jar_present = True
        jars = "%s,%s,%s,%s,%s" % (kafka_assembly_jar, flume_assembly_jar, mqtt_assembly_jar,
                                   mqtt_test_jar, kinesis_asl_assembly_jar)

    os.environ["PYSPARK_SUBMIT_ARGS"] = "--jars %s pyspark-shell" % jars
    testcases = [BasicOperationTests, WindowFunctionTests, StreamingContextTests,
                 CheckpointTests, KafkaStreamTests, FlumeStreamTests, FlumePollingStreamTests]

    if kinesis_jar_present is True:
        testcases.append(KinesisStreamTests)
    elif are_kinesis_tests_enabled is False:
        sys.stderr.write("Skipping all Kinesis Python tests as the optional Kinesis project was "
                         "not compiled with -Pkinesis-asl profile. To run these tests, "
                         "you need to build Spark with 'build/sbt -Pkinesis-asl assembly/assembly "
                         "streaming-kinesis-asl-assembly/assembly' or "
                         "'build/mvn -Pkinesis-asl package' before running this test.")
    else:
        raise Exception(
            ("Failed to find Spark Streaming Kinesis assembly jar in %s. "
             % kinesis_asl_assembly_dir) +
            "You need to build Spark with 'build/sbt -Pkinesis-asl "
            "assembly/assembly streaming-kinesis-asl-assembly/assembly'"
            "or 'build/mvn -Pkinesis-asl package' before running this test.")

    sys.stderr.write("Running tests: %s \n" % (str(testcases)))
    for testcase in testcases:
        sys.stderr.write("[Running %s]\n" % (testcase))
        tests = unittest.TestLoader().loadTestsFromTestCase(testcase)
        unittest.TextTestRunner(verbosity=2).run(tests)
