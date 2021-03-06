# coding:utf-8
"""HTTP Request Util"""

import sys
import os
import errno
import re
import socket
import ssl
import struct
import random
import OpenSSL
from . import clogging as logging
from select import select
from time import time, sleep
from .GlobalConfig import GC
from .compat.openssl import SSLConnection
from .compat import (
    PY3,
    Queue,
    thread,
    httplib,
    urlparse,
    xrange
    )
from .common.dns import dns, dns_resolve
from .common.proxy import parse_proxy
from .common import (
    cert_dir,
    NetWorkIOError,
    spawn_later
    )

class BaseHTTPUtil(object):
    """Basic HTTP Request Class"""

    use_openssl = 0
    ssl_ciphers = ':'.join([
                            'ECDHE-ECDSA-AES256-SHA',
                            'ECDHE-RSA-AES256-SHA',
                            'DHE-RSA-CAMELLIA256-SHA',
                            'DHE-DSS-CAMELLIA256-SHA',
                            'DHE-RSA-AES256-SHA',
                            'DHE-DSS-AES256-SHA',
                            'ECDH-RSA-AES256-SHA',
                            'ECDH-ECDSA-AES256-SHA',
                            'CAMELLIA256-SHA',
                            'AES256-SHA',
                            #'ECDHE-ECDSA-RC4-SHA',
                            #'ECDHE-ECDSA-AES128-SHA',
                            #'ECDHE-RSA-RC4-SHA',
                            #'ECDHE-RSA-AES128-SHA',
                            #'DHE-RSA-CAMELLIA128-SHA',
                            #'DHE-DSS-CAMELLIA128-SHA',
                            #'DHE-RSA-AES128-SHA',
                            #'DHE-DSS-AES128-SHA',
                            #'ECDH-RSA-RC4-SHA',
                            #'ECDH-RSA-AES128-SHA',
                            #'ECDH-ECDSA-RC4-SHA',
                            #'ECDH-ECDSA-AES128-SHA',
                            #'SEED-SHA',
                            #'CAMELLIA128-SHA',
                            #'RC4-SHA',
                            #'RC4-MD5',
                            #'AES128-SHA',
                            #'ECDHE-ECDSA-DES-CBC3-SHA',
                            #'ECDHE-RSA-DES-CBC3-SHA',
                            #'EDH-RSA-DES-CBC3-SHA',
                            #'EDH-DSS-DES-CBC3-SHA',
                            #'ECDH-RSA-DES-CBC3-SHA',
                            #'ECDH-ECDSA-DES-CBC3-SHA',
                            #'DES-CBC3-SHA',
                            'TLS_EMPTY_RENEGOTIATION_INFO_SCSV'])

    def __init__(self, use_openssl=None, cacert=None, ssl_ciphers=None):
        # http://docs.python.org/dev/library/ssl.html
        # http://www.openssl.org/docs/apps/ciphers.html
        self.cacert = cacert
        if ssl_ciphers:
            self.ssl_ciphers = ssl_ciphers
        if use_openssl:
            self.use_openssl = use_openssl
            self.set_ssl_option = self.set_openssl_option
            self.get_ssl_socket = self.get_openssl_socket
            self.get_peercert = self.get_openssl_peercert
        self.set_ssl_option()

    def set_ssl_option(self):
        self.ssl_context = ssl.SSLContext(GC.LINK_REMOTESSL)
        #validate
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        if self.cacert:
            self.ssl_context.load_verify_locations(self.cacert)
        #obfuscate
        self.ssl_context.set_ciphers(self.ssl_ciphers)

    def set_openssl_option(self):
        self.ssl_context = OpenSSL.SSL.Context(GC.LINK_REMOTESSL)
        #cache
        import binascii
        self.ssl_context.set_session_id(binascii.b2a_hex(os.urandom(10)))
        self.ssl_context.set_session_cache_mode(OpenSSL.SSL.SESS_CACHE_BOTH)
        #validate
        if self.cacert:
            self.ssl_context.load_verify_locations(self.cacert)
            self.ssl_context.set_verify(OpenSSL.SSL.VERIFY_PEER, lambda c, x, e, d, ok: ok)
        #obfuscate
        self.ssl_context.set_cipher_list(self.ssl_ciphers)

    def get_ssl_socket(self, sock, server_hostname=None):
        return self.ssl_context.wrap_socket(sock, do_handshake_on_connect=False, server_hostname=server_hostname)

    def get_openssl_socket(self, sock, server_hostname=None):
        ssl_sock = SSLConnection(self.ssl_context, sock)
        if server_hostname:
            ssl_sock.set_tlsext_host_name(server_hostname)
        return ssl_sock

    def get_peercert(self, sock):
        return OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_ASN1, sock.getpeercert(True))

    def get_openssl_peercert(self, sock):
        return sock.get_peer_certificate()

linkkeeptime = GC.LINK_KEEPTIME
gaekeeptime = GC.GAE_KEEPTIME
cachetimeout = GC.FINDER_MAXTIMEOUT * 1.2 / 1000
import collections
from .common import LRUCache
tcp_connection_time = LRUCache(256)
ssl_connection_time = LRUCache(256)
tcp_connection_cache = collections.defaultdict(collections.deque)
ssl_connection_cache = collections.defaultdict(collections.deque)

def check_tcp_connection_cache():
    '''check and close unavailable connection continued forever'''
    while True:
        sleep(10)
        #将键名放入元组
        keys = None
        while keys is None:
            try:
                keys = tuple(key for key in tcp_connection_cache)
            except:
                sleep(0.01)
        for cache_key in keys:
            keeptime = gaekeeptime if cache_key.startswith('google') else linkkeeptime
            cache = tcp_connection_cache[cache_key]
            try:
                while cache:
                    ctime, sock = cache.popleft()
                    if time()-ctime > keeptime:
                        sock.close()
                        continue
                    rd, _, ed = select([sock], [], [sock], 0.01)
                    if rd or ed:
                        sock.close()
                        continue
                    _, wd, ed = select([], [sock], [sock], cachetimeout)
                    if not wd or ed:
                        sock.close()
                        continue
                    cache.appendleft((ctime, sock))
                    break
            except IndexError:
                pass
            except Exception as e:
                if e.args[0] == 9:
                    pass
                else:
                    logging.error(u'链接池守护线程错误：%r', e)

def check_ssl_connection_cache():
    '''check and close unavailable connection continued forever'''
    while True:
        sleep(5)
        keys = None
        while keys is None:
            try:
                keys = tuple(key for key in ssl_connection_cache)
            except:
                sleep(0.01)
        for cache_key in keys:
            keeptime = gaekeeptime if cache_key.startswith('google') else linkkeeptime
            cache = ssl_connection_cache[cache_key]
            try:
                while cache:
                    ctime, ssl_sock = cache.popleft()
                    sock = ssl_sock.sock
                    if time()-ctime > keeptime:
                        sock.close()
                        continue
                    rd, _, ed = select([sock], [], [sock], 0.01)
                    if rd or ed:
                        sock.close()
                        continue
                    _, wd, ed = select([], [sock], [sock], cachetimeout)
                    if not wd or ed:
                        sock.close()
                        continue
                    cache.appendleft((ctime, ssl_sock))
                    break
            except IndexError:
                pass
            except Exception as e:
                if e.args[0] == 9:
                    pass
                else:
                    logging.error(u'链接池守护线程错误：%r', e)
thread.start_new_thread(check_tcp_connection_cache, ())
thread.start_new_thread(check_ssl_connection_cache, ())

connect_limiter = LRUCache(512)
def set_connect_start(ip):
    if ip not in connect_limiter:
        #只是限制同时正在发起的链接数，并不限制链接的总数，所以设定尽量小的数字
        connect_limiter[ip] = Queue.LifoQueue(3)
    connect_limiter[ip].put(True)

def set_connect_finish(ip):
    connect_limiter[ip].get()

class HTTPUtil(BaseHTTPUtil):
    """HTTP Request Class"""

    protocol_version = 'HTTP/1.1'

    def __init__(self, max_window=4, max_timeout=8, proxy='', ssl_ciphers=None, max_retry=2):
        # http://docs.python.org/dev/library/ssl.html
        # http://blog.ivanristic.com/2009/07/examples-of-the-information-collected-from-ssl-handshakes.html
        # http://src.chromium.org/svn/trunk/src/net/third_party/nss/ssl/sslenum.c
        # http://www.openssl.org/docs/apps/ciphers.html
        # openssl s_server -accept 443 -key CA.crt -cert CA.crt
        # set_ciphers as Modern Browsers
        self.max_window = max_window
        self.max_retry = max_retry
        self.max_timeout = max_timeout
        self.proxy = proxy
        #if self.proxy:
        #    dns_resolve = self.__dns_resolve_withproxy
        #    self.create_connection = self.__create_connection_withproxy
        #    self.create_ssl_connection = self.__create_ssl_connection_withproxy
        BaseHTTPUtil.__init__(self, GC.LINK_OPENSSL, os.path.join(cert_dir, 'cacert.pem'), ssl_ciphers)

    def create_connection(self, address, cache_key, timeout=None, source_address=None, **kwargs):
        def _create_connection(ipaddr, timeout, queobj):
            sock = None
            ip = ipaddr[0]
            try:
                # create a ipv4/ipv6 socket object
                sock = socket.socket(socket.AF_INET if ':' not in ipaddr[0] else socket.AF_INET6)
                # set reuseaddr option to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # set struct linger{l_onoff=1,l_linger=0} to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                # resize socket recv buffer 8K->32K to improve browser releated application performance
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32*1024)
                # disable nagle algorithm to send http request quickly.
                sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                # set a short timeout to trigger timeout retry more quickly.
                sock.settimeout(1)
                set_connect_start(ip)
                # start connection time record
                start_time = time()
                # TCP connect
                sock.connect(ipaddr)
                # set a normal timeout
                sock.settimeout(timeout)
                # record TCP connection time
                tcp_connection_time[ipaddr] = sock.tcp_time = time() - start_time
                # put socket object to output queobj
                sock.xip = ipaddr
                queobj.put(sock)
            except (socket.error, OSError) as e:
                # any socket.error, put Excpetions to output queobj.
                e.xip = ipaddr
                queobj.put(e)
                # reset a large and random timeout to the ipaddr
                tcp_connection_time[ipaddr] = self.max_timeout+random.random()
                # close tcp socket
                sock.close()
            finally:
                set_connect_finish(ip)
        def _close_connection(count, queobj, first_tcp_time):
            now = time()
            tcp_time_threshold = max(min(1.5, 1.5 * first_tcp_time), 0.5)
            cache = tcp_connection_cache[cache_key]
            for i in xrange(count):
                sock = queobj.get()
                if isinstance(sock, socket.socket):
                    if False and sock.tcp_time < tcp_time_threshold:
                        cache.append((now, sock))
                    else:
                        sock.close()

        try:
            keeptime = gaekeeptime if cache_key.startswith('google') else linkkeeptime
            cache = tcp_connection_cache[cache_key]
            while cache:
                ctime, sock = cache.pop()
                rd, _, ed = select([sock], [], [sock], 0.01)
                if rd or ed or time()-ctime > keeptime:
                    sock.close()
                else:
                    return sock
        except IndexError:
            pass
        result = None
        host, port = address
        addresses = [(x, port) for x in dns_resolve(host)]
        if port == 443:
            get_connection_time = lambda addr: tcp_connection_time.get(addr, False) or ssl_connection_time.get(addr, False)
        else:
            get_connection_time = lambda addr: tcp_connection_time.get(addr, False)
        for i in xrange(self.max_retry):
            addresseslen = len(addresses)
            addresses.sort(key=get_connection_time)
            if addresseslen > self.max_window:
                window = min((self.max_window+1)//2 + min(i, 1), addresseslen)
                addrs = addresses[:window] + random.sample(addresses[window:], self.max_window-window)
            else:
                addrs = addresses
            queobj = Queue.Queue()
            for addr in addrs:
                thread.start_new_thread(_create_connection, (addr, timeout, queobj))
            addrslen = len(addrs)
            for i in xrange(addrslen):
                result = queobj.get()
                if isinstance(result, Exception):
                    addr = result.xip
                    #临时移除 badip
                    try:
                        addresses.remove(addr)
                    except ValueError:
                        pass
                    if i == 0:
                        #only output first error
                        logging.warning(u'%s create_connection %r 返回 %r，重试', addr[0], host, result)
                else:
                    thread.start_new_thread(_close_connection, (addrslen-i-1, queobj, result.tcp_time))
                    return result
            if i == self.max_retry - 1:
                if result:
                    raise result

    def create_ssl_connection(self, address, cache_key, timeout=None, test=None, source_address=None, rangefetch=None, **kwargs):
        def _create_ssl_connection(ipaddr, timeout, queobj, retry=None):
            sock = None
            ssl_sock = None
            ip = ipaddr[0]
            try:
                # create a ipv4/ipv6 socket object
                sock = socket.socket(socket.AF_INET if ':' not in ipaddr[0] else socket.AF_INET6)
                # set reuseaddr option to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # set struct linger{l_onoff=1,l_linger=0} to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                # resize socket recv buffer 8K->32K to improve browser releated application performance
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32*1024)
                #sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
                # disable negal algorithm to send http request quickly.
                sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                # pick up the sock socket
                server_hostname = b'www.google.com' if address[0].endswith('.appspot.com') else None
                ssl_sock = self.get_ssl_socket(sock, server_hostname)
                # set a short timeout to trigger timeout retry more quickly.
                ssl_sock.settimeout(1)
                set_connect_start(ip)
                # start connection time record
                start_time = time()
                # TCP connect
                ssl_sock.connect(ipaddr)
                #connected_time = time()
                # set a short timeout to trigger timeout retry more quickly.
                ssl_sock.settimeout(timeout if test else 1.5)
                # SSL handshake
                ssl_sock.do_handshake()
                # set a normal timeout
                ssl_sock.settimeout(timeout)
                handshaked_time = time()
                # record TCP connection time
                #tcp_connection_time[ipaddr] = ssl_sock.tcp_time = connected_time - start_time
                # record SSL connection time
                ssl_connection_time[ipaddr] = ssl_sock.ssl_time = handshaked_time - start_time
                if test:
                    if ssl_sock.ssl_time > timeout:
                        raise socket.timeout(u'%d 超时' % int(ssl_sock.ssl_time*1000))
                # verify SSL certificate.
                if cache_key.startswith('google'):
                    cert = self.get_peercert(ssl_sock)
                    if not cert:
                        raise socket.error(u'没有获取到证书')
                    subject = cert.get_subject()
                    if subject.O != 'Google Inc':
                        raise ssl.SSLError(u'%s 证书的公司名称（%s）不是 "Google Inc"' % (address[0], subject.O))
                # sometimes, we want to use raw tcp socket directly(select/epoll), so setattr it to ssl socket.
                ssl_sock.sock = sock
                ssl_sock.xip = ipaddr
                if test:
                    ssl_connection_cache[cache_key].append((time(), ssl_sock))
                    return test.put((ipaddr[0], ssl_sock.ssl_time))
                # put ssl socket object to output queobj
                queobj.put(ssl_sock)
            except NetWorkIOError as e:
                # reset a large and random timeout to the ipaddr
                ssl_connection_time[ipaddr] = self.max_timeout + random.random()
                # close tcp socket
                sock.close()
                # any socket.error, put Excpetions to output queobj.
                e.xip = ipaddr
                if test:
                    if not retry and e.args == (-1, 'Unexpected EOF'):
                        return _create_ssl_connection(ipaddr, timeout, test, True)
                    return test.put(e)
                queobj.put(e)
            finally:
                set_connect_finish(ip)

        def _close_ssl_connection(count, queobj, first_ssl_time):
            now = time()
            ssl_time_threshold = max(min(1.5, 1.5 * first_ssl_time), 1.0)
            cache = ssl_connection_cache[cache_key]
            for i in xrange(count):
                ssl_sock = queobj.get()
                if isinstance(ssl_sock, (SSLConnection, ssl.SSLSocket)):
                    if ssl_sock.ssl_time < ssl_time_threshold:
                        cache.append((now, ssl_sock))
                    else:
                        ssl_sock.sock.close()

        if test:
            return _create_ssl_connection(address, timeout, test)
        try:
            keeptime = gaekeeptime if cache_key.startswith('google') else linkkeeptime
            cache = ssl_connection_cache[cache_key]
            while cache:
                ctime, ssl_sock = cache.pop()
                rd, _, ed = select([ssl_sock.sock], [], [ssl_sock.sock], 0.01)
                if rd or ed or time()-ctime > keeptime:
                    ssl_sock.sock.close()
                else:
                    ssl_sock.settimeout(timeout)
                    return ssl_sock
        except IndexError:
            pass
        host, port = address
        result = None
        addresses = [(x, port) for x in dns_resolve(host)]
        for i in xrange(self.max_retry):
            addresseslen = len(addresses)
            addresses.sort(key=lambda addr: ssl_connection_time.get(addr, False))
            if rangefetch:
                #按线程数量获取排序靠前的 IP
                addrs = addresses[:GC.AUTORANGE_THREADS+1]
            else:
                max_window = self.max_window
                if addresseslen > max_window:
                    window = min((max_window+1)//2 + min(i, 1), addresseslen)
                    addrs = addresses[:window] + random.sample(addresses[window:], max_window-window)
                else:
                    addrs = addresses
            queobj = Queue.Queue()
            for addr in addrs:
                thread.start_new_thread(_create_ssl_connection, (addr, timeout, queobj))
            addrslen = len(addrs)
            for i in xrange(addrslen):
                result = queobj.get()
                if isinstance(result, Exception):
                    addr = result.xip
                    #临时移除 badip
                    try:
                        addresses.remove(addr)
                    except ValueError:
                        pass
                    if i == 0:
                        #only output first error
                        logging.warning(u'%s create_ssl_connection %r 返回 %r，重试', addr[0], host, result)
                else:
                    thread.start_new_thread(_close_ssl_connection, (addrslen-i-1, queobj, result.ssl_time))
                    return result
            if i == self.max_retry - 1:
                if result:
                    raise result

    def __create_connection_withproxy(self, address, timeout=None, source_address=None, **kwargs):
        host, port = address
        logging.debug('__create_connection_withproxy connect (%r, %r)', host, port)
        _, proxyuser, proxypass, proxyaddress = parse_proxy(self.proxy)
        try:
            try:
                dns_resolve(host)
            except (socket.error, OSError):
                pass
            proxyhost, _, proxyport = proxyaddress.rpartition(':')
            sock = socket.create_connection((proxyhost, int(proxyport)))
            if host in dns:
                hostname = random.choice(dns[host])
            elif host.endswith('.appspot.com'):
                hostname = 'www.google.com'
            else:
                hostname = host
            request_data = 'CONNECT %s:%s HTTP/1.1\r\n' % (hostname, port)
            if proxyuser and proxypass:
                request_data += 'Proxy-authorization: Basic %s\r\n' % base64.b64encode(('%s:%s' % (proxyuser, proxypass)).encode()).decode().strip()
            request_data += '\r\n'
            sock.sendall(request_data)
            response = httplib.HTTPResponse(sock)
            response.begin()
            if response.status >= 400:
                logging.error('__create_connection_withproxy return http error code %s', response.status)
                sock = None
            return sock
        except Exception as e:
            logging.error('__create_connection_withproxy error %s', e)
            raise

    def __create_ssl_connection_withproxy(self, address, timeout=None, source_address=None, **kwargs):
        host, port = address
        logging.debug('__create_ssl_connection_withproxy connect (%r, %r)', host, port)
        try:
            sock = self.__create_connection_withproxy(address, timeout, source_address)
            ssl_sock = self.get_ssl_socket(sock)
            ssl_sock.sock = sock
            return ssl_sock
        except Exception as e:
            logging.error('__create_ssl_connection_withproxy error %s', e)
            raise

    def _request(self, sock, method, path, protocol_version, headers, payload, bufsize=8192, crlf=None):
        #need_crlf = bool(crlf)
        need_crlf = False
        if need_crlf:
            fakehost = 'www.' + ''.join(random.choice(('bcdfghjklmnpqrstvwxyz','aeiou')[x&1]) for x in xrange(random.randint(5,20))) + random.choice(['.net', '.com', '.org'])
            request_data = 'GET / HTTP/1.1\r\nHost: %s\r\n\r\n\r\n\r\r' % fakehost
        else:
            request_data = ''
        request_data += '%s %s %s\r\n' % (method, path, protocol_version)
        request_data += ''.join('%s: %s\r\n' % (k.title(), v) for k, v in headers.items())
        if self.proxy:
            _, username, password, _ = parse_proxy(self.proxy)
            if username and password:
                request_data += 'Proxy-Authorization: Basic %s\r\n' % base64.b64encode(('%s:%s' % (username, password)).encode()).decode().strip()
        request_data += '\r\n'
        if not isinstance(request_data, bytes):
            request_data = request_data.encode()

        sock.sendall(request_data + payload)
        #if isinstance(payload, bytes):
        #    sock.sendall(request_data.encode() + payload)
        #elif hasattr(payload, 'read'):
        #    sock.sendall(request_data)
        #    sock.sendall(payload.read())
        #else:
        #    raise TypeError('request(payload) must be a string or buffer, not %r' % type(payload))

        #if need_crlf:
        #    try:
        #        response = httplib.HTTPResponse(sock)
        #        response.begin()
        #        response.read()
        #    except Exception as e:
        #        logging.exception('crlf skip read')
        #        raise e

        try:
            response = httplib.HTTPResponse(sock) if PY3 else httplib.HTTPResponse(sock, buffering=True)
            #exc_clear()
            response.begin()
        except Exception as e:
            #这里有时会捕捉到奇怪的异常，找不到来源路径
            # py2 的 raise 不带参数会导致捕捉到错误的异常，但使用 exc_clear 或换用 py3 还是会出现
            if hasattr(e, 'xip'):
                #logging.warning('4444 %r | %r | %r', sock.getpeername(), sock.xip, e.xip)
                del e.xip
            raise e

        response.xip =  sock.xip
        response.sock = sock
        return response

    def request(self, request_params, payload=None, headers={}, bufsize=8192, crlf=None, connection_cache_key=None, timeout=None, rangefetch=None, realurl=None):
        ssl = request_params.ssl
        host = request_params.host
        port = request_params.port
        method = request_params.command
        url = request_params.url

        if 'Host' not in headers:
            headers['Host'] = host
        if payload:
            if not isinstance(payload, bytes):
                payload = payload.encode()
            if 'Content-Length' not in headers:
                headers['Content-Length'] = str(len(payload))

        for i in xrange(self.max_retry):
            sock = None
            ssl_sock = None
            ip = ''
            try:
                if ssl:
                    ssl_sock = self.create_ssl_connection((host, port), connection_cache_key, timeout or self.max_timeout, rangefetch=rangefetch)
                    crlf = 0
                else:
                    sock = self.create_connection((host, port), connection_cache_key, timeout or self.max_timeout)
                if ssl_sock or sock:
                    response =  self._request(ssl_sock or sock, method, request_params.path, self.protocol_version, headers, payload, bufsize=bufsize, crlf=crlf)
                    return response
            except Exception as e:
                if ssl_sock:
                    ip = ssl_sock.xip
                    ssl_sock.sock.close()
                elif sock:
                    ip = sock.xip
                    sock.close()
                if hasattr(e, 'xip'):
                    ip = e.xip
                    logging.warning(u'%s create_%s connection %r 失败：%r', ip[0], '' if port == 80 else 'ssl_', realurl or url, e)
                else:
                    logging.warning(u'%s _request "%s %s" 失败：%r', ip[0], method, realurl or url, e)
                    if realurl:
                        ssl_connection_time[ip] = self.max_timeout + random.random()
                if not realurl and e.args[0] == errno.ECONNRESET:
                    raise e
            #if i == self.max_retry - 1:
            #    logging.warning(u'%s request "%s %s" 失败', ip[0], method, realurl or url)

# Google video ip can act as Google FrontEnd if cipher suits not include
# RC4-SHA
# AES128-GCM-SHA256
# ECDHE-RSA-RC4-SHA
# ECDHE-RSA-AES128-GCM-SHA256
#不安全 cipher
# AES128-SHA
# ECDHE-RSA-AES128-SHA
gws_ciphers = ':'.join([
                        #defaultTLS
                        ##'AES128-SHA',
                        #'AES256-SHA',
                        ##'AES128-GCM-SHA256',
                        'AES256-GCM-SHA384',
                        #'ECDHE-ECDSA-AES128-SHA',
                        'ECDHE-ECDSA-AES256-SHA',
                        ##'ECDHE-RSA-AES128-SHA',
                        'ECDHE-RSA-AES256-SHA',
                        ##'ECDHE-RSA-AES128-GCM-SHA256',
                        'ECDHE-RSA-AES256-GCM-SHA384',
                        'ECDHE-ECDSA-AES128-GCM-SHA256',
                        'ECDHE-ECDSA-AES256-GCM-SHA384',
                        #defaultTLS ex
                        'AES128-SHA256',
                        'ECDHE-RSA-AES128-SHA256',
                        #mixinCiphers
                        ##'RC4-SHA',
                        #'DES-CBC3-SHA',
                        ##'ECDHE-RSA-RC4-SHA',
                        #'ECDHE-RSA-DES-CBC3-SHA',
                        #'ECDHE-ECDSA-RC4-SHA',
                        #mixinCiphers ex
                        'AES256-SHA256',
                        'TLS_EMPTY_RENEGOTIATION_INFO_SCSV'])
def_ciphers = ssl._DEFAULT_CIPHERS
res_ciphers = ssl._RESTRICTED_SERVER_CIPHERS

# max_window=4, max_timeout=8, proxy='', ssl_ciphers=None, max_retry=2
http_gws = HTTPUtil(GC.LINK_WINDOW, GC.LINK_TIMEOUT, GC.proxy, gws_ciphers)
http_nor = HTTPUtil(GC.LINK_WINDOW, GC.LINK_FWDTIMEOUT, GC.proxy, res_ciphers)
