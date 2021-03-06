# coding:utf-8
"""Range Fetch Util"""


import re
import threading
import random
from . import clogging as logging
from time import time, sleep
from .compat import Queue, thread, urlparse, xrange
from .common import spawn_later
from .GAEFetch import qGAE, gae_urlfetch
from .GlobalConfig import GC
from .HTTPUtil import ssl_connection_cache
from .GAEUpdata import testip, testallgaeip

getrange = re.compile(r'bytes (\d+)-(\d+)/(\d+)').search

class RangeFetch(object):
    """Range Fetch Class"""

    maxsize = GC.AUTORANGE_MAXSIZE or 1024*1024*4
    bufsize = GC.AUTORANGE_BUFSIZE or 8192
    threads = GC.AUTORANGE_THREADS or 2
    minip = max(threads-2, 3)
    lowspeed = GC.AUTORANGE_LOWSPEED or 1024*32
    timeout = min(max(GC.LINK_TIMEOUT-2, 1.5), 3)
    sleeptime = GC.FINDER_MAXTIMEOUT/500.0

    def __init__(self, handler, url, headers, payload, response):
        self.tLock = threading.Lock()
        self.expect_begin = 0
        self._stopped = False
        self._last_app_status = {}
        self.lastupdata = testip.lastupdata
        self.iplist = GC.IPLIST_MAP['google_gws'][:]
        self.appids = Queue.Queue()
        for id in GC.GAE_APPIDS:
            self.appids.put(id)

        self.handler = handler
        self.write = handler.write
        self.command = handler.command
        self.host = handler.host
        self.url = url
        self.headers = headers
        self.payload = payload
        self.response = response

    def fetch(self):
        response_status = self.response.status
        response_headers = dict((k.title(), v) for k, v in self.response.getheaders())
        content_range = response_headers['Content-Range']
        start, end, length = tuple(int(x) for x in getrange(content_range).group(1, 2, 3))
        if start == 0:
            response_status = 200
            response_headers['Content-Length'] = str(length)
            del response_headers['Content-Range']
        else:
            response_headers['Content-Range'] = 'bytes %s-%s/%s' % (start, end, length)
            response_headers['Content-Length'] = str(length-start)

        logging.info(u'>>>>>>>>>>>>>>> RangeFetch 开始 %r %d-%d', self.url, start, end)
        self.write(('HTTP/1.1 %s\r\n%s\r\n' % (response_status, ''.join('%s: %s\r\n' % (k, v) for k, v in response_headers.items()))))

        #开始多线程时先测试一遍 IP
        sleeptime = self.sleeptime if testallgaeip(True) else 0

        data_queue = Queue.PriorityQueue()
        range_queue = Queue.PriorityQueue()
        range_queue.put((start, end))
        # py2 弃用，xrange 参数太大时会出错，range 不出错但耗时太多
        #for begin in range(end+1, length, self.maxsize):
        #    range_queue.put((begin, min(begin+self.maxsize-1, length-1)))
        a = end + 1
        b = end
        n = (length-a)//self.maxsize
        for i in xrange(n):
            b += self.maxsize
            range_queue.put((a, b))
            a = b + 1
        if length > a:
            range_queue.put((a, length-1))

        for i in xrange(self.threads):
            range_delay_size = int((self.threads-i) * self.maxsize * self.threads * 0.66)
            spawn_later(sleeptime if i else 0, self.__fetchlet, range_queue, data_queue, range_delay_size, i+1)
        has_peek = hasattr(data_queue, 'peek')
        peek_timeout = 120
        self.expect_begin = start
        while self.expect_begin < length:
            try:
                if has_peek:
                    begin, data = data_queue.peek(timeout=peek_timeout)
                    if self.expect_begin == begin:
                        data_queue.get()
                    elif self.expect_begin < begin:
                        sleep(0.1)
                        continue
                    else:
                        logging.error('RangeFetch Error: begin(%r) < expect_begin(%r), quit.', begin, self.expect_begin)
                        break
                else:
                    begin, data = data_queue.get(timeout=peek_timeout)
                    if self.expect_begin == begin:
                        pass
                    elif self.expect_begin < begin:
                        data_queue.put((begin, data))
                        sleep(0.1)
                        continue
                    else:
                        logging.error('RangeFetch Error: begin(%r) < expect_begin(%r), quit.', begin, self.expect_begin)
                        break
            except Queue.Empty:
                logging.error('data_queue peek timeout, break')
                break
            try:
                self.write(data)
                self.expect_begin += len(data)
            except Exception as e:
                logging.info(u'RangeFetch %r 本地链接断开：%r', self.host, e)
                break
        else:
            logging.info(u'RangeFetch %r 成功完成', self.host)
        self._stopped = True

    def address_string(self, response=None):
        return self.handler.address_string(response)

    def __fetchlet(self, range_queue, data_queue, range_delay_size, threadorder):
        headers = dict((k.title(), v) for k, v in self.headers.items())
        headers['Connection'] = 'close'
        while True:
            try:
                with self.tLock:
                    if self.lastupdata != testip.lastupdata:
                        self.lastupdata = testip.lastupdata
                        self.iplist = GC.IPLIST_MAP['google_gws'][:]
                noerror = True
                response = None
                starttime = None
                appid = None
                if self._stopped: return
                try:
                    start, end = range_queue.get(timeout=1)
                    headers['Range'] = 'bytes=%d-%d' % (start, end)
                    appid = self.appids.get()
                    if self._last_app_status.get(appid, 200) >= 500:
                        sleep(2)
                    while start - self.expect_begin > self.maxsize and data_queue.qsize() * self.bufsize > range_delay_size:
                        sleep(0.1)
                    if self.response:
                        qGAE.get()
                        response = self.response
                        self.response = None
                    else:
                        response = gae_urlfetch(self.command, self.url, headers, self.payload, appid, timeout=self.timeout, rangefetch=True)
                    if response:
                        if response.xip[0] in self.iplist:
                            self._last_app_status[appid] = response.app_status
                            realstart = start
                            starttime = time()
                        else:
                            range_queue.put((start, end))
                            continue
                except Queue.Empty:
                    continue
                except Exception as e:
                    logging.warning("Response %r in __fetchlet", e)
                    range_queue.put((start, end))
                    continue
                if not response:
                    logging.warning('RangeFetch %s return %r', headers['Range'], response)
                    range_queue.put((start, end))
                elif response.app_status != 200:
                    logging.warning('%s Range Fetch "%s %s" %s return %s', self.address_string(response), self.command, self.url, headers['Range'], response.app_status)
                    range_queue.put((start, end))
                elif response.getheader('Location'):
                    self.url = urlparse.urljoin(self.url, response.getheader('Location'))
                    logging.info('%s RangeFetch Redirect(%r)', self.address_string(response), self.url)
                    range_queue.put((start, end))
                elif 200 <= response.status < 300:
                    content_range = response.getheader('Content-Range')
                    if not content_range:
                        logging.warning('%s RangeFetch "%s %s" return Content-Range=%r: response headers=%r', self.address_string(response), self.command, self.url, content_range, response.getheaders())
                        range_queue.put((start, end))
                        continue
                    content_length = int(response.getheader('Content-Length', 0))
                    logging.info('%s >>>>>>>>>>>>>>> [%s: %s] %s %s', self.address_string(response), self.host, threadorder, content_length, content_range)
                    try:
                        data = response.read(self.bufsize)
                        while data:
                            if self._stopped: return
                            data_queue.put((start, data))
                            start += len(data)
                            data = response.read(self.bufsize)
                    except Exception as e:
                        noerror = False
                        with self.tLock:
                            if response.xip[0] in self.iplist and len(self.iplist) > self.minip:
                                self.iplist.remove(response.xip[0])
                                logging.warning(u'RangeFetch 移除故障 ip %s', response.xip[0])
                        logging.warning(u'%s RangeFetch "%s %s" %s 失败：%r', self.address_string(response), self.command, self.url, headers['Range'], e)
                    if start < end + 1:
                        logging.warning(u'%s RangeFetch "%s %s" 重试 %s-%s', self.address_string(response), self.command, self.url, start, end)
                        range_queue.put((start, end))
                        continue
                    logging.info(u'%s >>>>>>>>>>>>>>> 成功接收到 %d 字节', self.address_string(response), start - 1)
                else:
                    logging.error(u'%s RangeFetch %r 返回 %s', self.address_string(response), self.url, response.status)
                    range_queue.put((start, end))
                    appid = None
            except Exception as e:
                logging.exception(u'RangeFetch._fetchlet 错误：%r', e)
                raise
            finally:
                qGAE.put(True)
                if appid:
                    self.appids.put(appid)
                if response:
                    response.close()
                    if noerror and not self._stopped:
                        #移除慢速 ip
                        with self.tLock:
                            if response.xip[0] in self.iplist and starttime and len(self.iplist) > self.minip and (start-realstart)/(time()-starttime) < self.lowspeed:
                                self.iplist.remove(response.xip[0])
                                logging.warning(u'RangeFetch 移除慢速 ip %s', response.xip[0])
                        #放入套接字缓存
                        ssl_connection_cache['google_gws:443'].append((time(), response.sock))
