# coding:utf-8
'''Auto check and updata GAE IP'''

import threading
from . import clogging as logging
from time import time, sleep, strftime
from .compat import (
    thread,
    ConfigParser,
    xrange,
    Queue
    )
from .common import config_dir
from .GlobalConfig import GC
from .ProxyServer import network_test
from .HTTPUtil import http_gws
from .GAEFinder import (
    g as finder,
    timeToDelay,
    getgaeip,
    savestatistics,
    #savebadlist
    )

lLock = threading.Lock()
tLock = threading.Lock()

class testip():
    running = False
    lastactive = None
    queobj = Queue.Queue()
    lastupdata = time()
    lasttest = lastupdata - 30

def removeip(ip):
    with lLock:
        for name in GC.IPLIST_MAP:
            try:
                GC.IPLIST_MAP[name].remove(ip)
            except:
                pass

#def addtoblocklist(ip):
#    removeip(ip)
#    finder.baddict[ip] = GC.FINDER_TIMESBLOCK+1, int(time())
#    finder.reloadlist = True
#    savebadlist()

def _refreship(gaeip):
    with lLock:
        for name in gaeip:
            GC.IPLIST_MAP[name][:] = gaeip[name] + GC.IPLIST_MAP[name]
    testip.lastupdata = time()

def refreship(needcom=None, threads=None):
    threading.current_thread().setName('Ping-IP')
    #检测当前 IP 并搜索新的 IP
    network_test()
    if needcom is None:
        needcom = max(GC.FINDER_MINIPCNT/3 - len(GC.IPLIST_MAP['google_com']), 0) 
    gaeip = getgaeip(GC.IPLIST_MAP['google_gws'], needcom, threads)
    #更新 IP
    if gaeip and gaeip['google_gws']:
        _refreship(gaeip)
        #更新 proxy.user.ini
        cf = ConfigParser()
        cf.read(GC.CONFIG_IPDB)
        for name in gaeip:
            cf.set("iplist", name, '|'.join(x for x in GC.IPLIST_MAP[name]))
        cf.write(open(GC.CONFIG_IPDB, "w"))
        logging.test(u'GAE IP 更新完毕')
    if len(GC.IPLIST_MAP['google_gws']) < GC.FINDER_MINIPCNT:
        logging.warning(u'没有检测到足够数量符合要求的 GAE IP，请重新设定参数！')
    #更新完毕
    #sleep(10)
    updataip.running = False

def updataip(needcom=None, threads=None):
    with tLock:
        if updataip.running: #是否更新
            return
        updataip.running = True
    thread.start_new_thread(refreship, (needcom, threads))
updataip.running = False

def gettimeout():
    nowtime = int(strftime('%H'))
    timeout = max(GC.FINDER_MAXTIMEOUT*1.3, 1000) + min(len(GC.IPLIST_MAP['google_gws']), 20)*10 + timeToDelay[nowtime]
    return int(timeout)

def testipuseable(ip):
    timeout = gettimeout()
    queobj = Queue.Queue()
    http_gws.create_ssl_connection((ip, 443), 'google_gws:443', timeout/1000.0, queobj)
    result = queobj.get()
    if isinstance(result, Exception):
        removeip(ip)
        logging.warning(u'IP：%r 暂时不可用，已经删除', ip)

def _testallgaeip():
    iplist = GC.IPLIST_MAP['google_gws']
    niplist = len(iplist or [])
    if niplist == 0:
        return updataip()
    badip = set()
    timeout = gettimeout()
    logging.test(u'连接测试开始，超时：%d 毫秒', timeout)
    network_test()
    testip.queobj.queue.clear()
    for ip in iplist:
        thread.start_new_thread(http_gws.create_ssl_connection, ((ip, 443), 'google_gws:443', timeout/1000.0, testip.queobj))
    for i in xrange(niplist):
        result = testip.queobj.get()
        if isinstance(result, Exception):
            ip = result.xip[0]
            logging.warning(u'测试失败 %s：%s' % ('.'.join(x.rjust(3) for x in ip.split('.')), result.args[0]))
            badip.add(ip)
        else:
            logging.test(u'测试连接 %s: %d' %('.'.join(x.rjust(3) for x in result[0].split('.')), int(result[1]*1000)))
    #删除 bad IP
    nbadip = len(badip)
    if nbadip > 0:
        for ip in badip:
            removeip(ip)
    logging.test(u'连接测试完毕%s', u'，Bad IP 已删除' if nbadip > 0 else '')
    testip.lasttest = time()
    testip.lastactive = testip.lasttest
    testip.running = False
    #刷新开始
    needgws = max(GC.FINDER_MINIPCNT - len(GC.IPLIST_MAP['google_gws']), 0)
    needcom = max(GC.FINDER_MINIPCNT/3 - len(GC.IPLIST_MAP['google_com']), 0)
    if needgws > 0 or needcom > 0:
        updataip(needcom, needgws + needcom*2 + 1)

def testallgaeip(force=False):
    with tLock:
        if updataip.running:
            return
        elif force:
            if testip.running == 9:
                return
            while testip.running == 1:
                sleep(0.2)
        elif testip.running:
            return
        testip.running = 9
    thread.start_new_thread(_testallgaeip, ())
    return True

def testonegaeip(again=False):
    if not again:
        with tLock:
            if updataip.running or testip.running:
                return
            testip.running = 1
    ip = GC.IPLIST_MAP['google_gws'][-1]
    timeout = gettimeout()
    badip = False
    statistics = finder.statistics
    network_test()
    testip.queobj.queue.clear()
    http_gws.create_ssl_connection((ip, 443), 'google_gws:443', timeout/1000.0, testip.queobj)
    result = testip.queobj.get()
    if isinstance(result, Exception):
        logging.warning(u'测试失败（超时：%d 毫秒）%s：%s，Bad IP 已删除' % (timeout,  '.'.join(x.rjust(3) for x in ip.split('.')), result.args[0]))
        removeip(ip)
        badip = True
        for ipdict in statistics:
            if ip in ipdict:
                ipdict[ip] = ipdict[ip][0], ipdict[ip][1]+1
            else:
                ipdict[ip] = 0, 1
    else:
        logging.test(u'测试连接（超时：%d 毫秒）%s: %d' %(timeout,  '.'.join(x.rjust(3) for x in result[0].split('.')), int(result[1]*1000)))
        GC.IPLIST_MAP['google_gws'].insert(0, GC.IPLIST_MAP['google_gws'].pop())
        #调高 com 权重
        addn = 2 if ip in GC.IPLIST_MAP['google_com'] else 1
        for ipdict in statistics:
            if ip in ipdict:
                ipdict[ip] = ipdict[ip][0]+addn, ipdict[ip][1]
            else:
                ipdict[ip] = addn, 0
    savestatistics()
    testip.lasttest = time()
    #刷新开始
    needgws = max(GC.FINDER_MINIPCNT - len(GC.IPLIST_MAP['google_gws']), 0)
    needcom = max(GC.FINDER_MINIPCNT/3 - len(GC.IPLIST_MAP['google_com']), 0)
    if needgws > 0 or needcom > 0:
        testip.running = False
        updataip(needcom, needgws + needcom*2 + 1)
    elif badip:
        testonegaeip(True)
    testip.running = False

def testipserver():
    while True:
        now = time()
        try:
            if not testip.lastactive:                    #启动时
                testallgaeip()
            elif ((now - testip.lastactive > 6 or # X 秒钟未使用
                    now - testip.lasttest > 30) and  #强制 X 秒钟检测
                    #and not GC.PROXY_ENABLE              #无代理
                    now - testip.lasttest > 120/(len(GC.IPLIST_MAP['google_gws']) or 1)): #强制 x 秒间隔
                testonegaeip()
        except Exception as e:
            logging.error(u' IP 测试守护线程错误：%r', e)
        finally:
            sleep(2)
