# coding:utf-8

import os
import re
from functools import partial
from .compat import ConfigParser
from .common import config_dir, isipv4, isipv6
from .GlobalConfig import GC

BLOCK     = 1
FORWARD   = 2
DIRECT    = 3
GAE       = 4
FAKECERT  = 5
PROXY     = 6
REDIRECT  = 7
IREDIRECT = 8

numToAct = {
    BLOCK     : 'do_BLOCK',
    FORWARD   : 'do_FORWARD',
    DIRECT    : 'do_DIRECT',
    REDIRECT  : 'do_REDIRECT',
    IREDIRECT : 'do_IREDIRECT',
    PROXY     : 'do_PROXY',
    FAKECERT  : 'do_DIRECT',
    GAE       : 'do_GAE',
}
numToSSLAct = {
    BLOCK     : 'do_BLOCK',
    FORWARD   : 'do_FORWARD',
    DIRECT    : 'do_FAKECERT',
    REDIRECT  : 'do_FAKECERT',
    IREDIRECT : 'do_FAKECERT',
    PROXY     : 'do_PROXY',
    FAKECERT  : 'do_FAKECERT',
    GAE       : 'do_FAKECERT',
}
actToNum = {
    'BLOCK'     : BLOCK,
    'FORWARD'   : FORWARD,
    'DIRECT'    : DIRECT,
    'REDIRECT'  : REDIRECT,
    'IREDIRECT' : IREDIRECT,
    'PROXY'     : PROXY,
    'FAKECERT'  : FAKECERT,
    'GAE'       : GAE,
}

isfiltername = re.compile(r'(?P<order>\d+)-(?P<action>\w+)').match
if GC.LINK_PROFILE == 'ipv4':
    pickip = re.compile(r'(?<=\s|\|)(?:\d+\.){3}\d+(?=$|\s|\|)').findall
    ipnotuse = isipv6
elif GC.LINK_PROFILE == 'ipv46':
    pickip = re.compile(r'(?<=\s|\|)((?:\d+\.){3}\d+|(?:(?:[a-f\d]{1,4}:){1,6}|:)(?:[a-f\d]{1,4})?(?::[a-f\d]{1,4}){1,6})(?=$|\s|\|)').findall
    #还要使用字符名称，所以不用验证
    ipnotuse = lambda x: False
elif GC.LINK_PROFILE == 'ipv6':
    pickip = re.compile(r'(?<=\s|\|)(?:(?:[a-f\d]{1,4}:){1,6}|:)(?:[a-f\d]{1,4})?(?::[a-f\d]{1,4}){1,6}(?=$|\s|\|)').findall
    ipnotuse = isipv4

class classlist(list): pass

ACTION_FILTERS = classlist()
ACTION_FILTERS.reset = False
CONFIG = ConfigParser()
CONFIG._optcre = re.compile(r'(?P<option>[^\s]+)(?P<vi>\s+=)?\s*(?P<value>.*)')
ACTION_FILTERS.CONFIG_FILENAME = os.path.join(config_dir, 'ActionFilter.ini')
CONFIG.read(ACTION_FILTERS.CONFIG_FILENAME)

sections = CONFIG.sections()
sections.sort()
for s in sections:
    try:
        order, action = isfiltername(s).group('order', 'action')
    except:
        continue
    action = action.upper()
    if action not in actToNum:
        continue
    #order = int(order)
    filters = classlist()
    filters.action = actToNum[action]
    #print('[%s]' % s, filters.action)
    for k, v in CONFIG.items(s):
        scheme = ''
        if k.find('://', 0, 9) > 0 :
            scheme, _, k = k.partition('://')
        if  '/' in  k:
            host, _, path = k.partition('/')
        else:
            host, path = k, ''
        if host.find('@') == 0:
            host = re.compile(host[1:]).search
        if path.find('@') == 0:
            path = re.compile(path[1:]).search
        if v and filters.action in (2, 3):
            if '|' in v:
                v = pickip(' '+v.lower()) or ''
            elif ipnotuse(v):
                v = ''
        elif filters.action in (7, 8) and '>>' in v:
            patterns, _, replaces = v.partition('>>')
            patterns = patterns.rstrip(' \t')
            replaces = replaces.lstrip(' \t')
            if patterns[0] == '@':
                patterns = patterns[1:].lstrip(' \t')
                if replaces[0] == '@':
                    replaces = replaces[1:].lstrip(' \t')
                    v = partial(re.compile(patterns).sub, replaces), True
                else:
                    v = partial(re.compile(patterns).sub, replaces), False
            else:
                if replaces[0] == '@':
                    replaces = replaces[1:].lstrip(' \t')
                    v = (patterns, replaces, 1), True
                else:
                    v = (patterns, replaces, 1), False
        #print(host, path, v)
        #print('@'+host.__self__.pattern if isinstance(host, dir.__class__) else host,
        #      '@'+url.__self__.pattern if isinstance(url, dir.__class__) else url,
        #      v)
        filters.append((scheme.lower(), host, path, v))
    ACTION_FILTERS.append(filters)
