#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
download.py
_________
"""

import os
import io
import errno
import time
import datetime
import logging
from logging.handlers import RotatingFileHandler
import requests
import pymysql
import paramiko
import yaml
import hashlib
from pytz import timezone


# virustotal.hunt 엔 있고, depot 엔 저장되지 않은(depot.path == NULL) 샘플들만 다운로드 받는다
SELECT_SAMPLES_NOT_STORED = 'SELECT virustotal.md5 FROM virustotal ' \
                            'where (virustotal.hunt is not null or virustotal.report is not null) ' \
                            'and not exists (select depot.md5 from depot where virustotal.md5=depot.md5)'
UPDATE_PATH = 'UPDATE depot SET path=%s WHERE md5=%s'


class VTDownloader:

    def __init__(self, config):
        self.config = config
        self.api = config['virustotal']['api']
        self.logger = logging.getLogger(config['log']['logname'])
        self.trigger = False
        self.conn = None
        self.cur = None
        self.sftp = None
        try:
            self.conn = pymysql.connect(
                host=config['mysql']['host'],
                port=config['mysql']['port'],
                database=config['mysql']['database'],
                user=config['mysql']['user'],
                passwd=config['mysql']['passwd'],
            )
            self.cur = self.conn.cursor()
        except Exception:
            self.logger.critical('MySql connection error')
            raise

        try:
            self.sftp = self.__conn_sftp(config['sftp'])
        except Exception:
            self.logger.critical('SFTP connection error')
            raise

    def __conn_sftp(self, config):
        host = config['host']
        port = config['port']
        user = config['user']
        passwd = config['passwd']

        sock = (host, port)

        t = paramiko.Transport(sock)

        # Transport 로 서버 접속
        t.connect(username=user, password=passwd)

        # 클라이언트 초기화
        sftp = paramiko.SFTPClient.from_transport(t)

        return sftp

    def __store_sftp(self, md5, binary):
        def exists(path):
            try:
                self.sftp.stat(path)
            except IOError as e:
                if e.errno == errno.ENOENT:
                    return False
                else:
                    raise
            else:
                return True

        root = 'md5'
        prefixes = [md5[:2], md5[2:4]]
        remote_dir = '/'.join([root, ] + prefixes)
        remote_path = '/'.join([remote_dir, md5])

        # 폴더 없으면 생성
        if not exists(remote_dir):
            path_to_remote_dir = root
            for prefix in prefixes:
                path_to_remote_dir = '/'.join([path_to_remote_dir, prefix])
                try:
                    self.sftp.mkdir(path_to_remote_dir)  # MD5 해쉬값, 두 글자단위, 2 depth 로 폴더생성
                except Exception:
                    pass

        # 이미 파일 있으면 삭제
        if exists(remote_path):
            self.sftp.remove(remote_path)

        fp = io.BytesIO(binary)
        self.sftp.putfo(fp, remote_path)
        return remote_path

    def work(self):

        # set timezone explicit
        tz = timezone('Asia/Seoul')
        self.logger.info('current utc time is %s' % str(datetime.datetime.utcnow()))
        self.logger.info('current local time is %s' % str(datetime.datetime.now(tz=tz)))

        while True:
            # 20시 이후에만 다운로드하자
            time.sleep(60)
            currtime = datetime.datetime.time(datetime.datetime.now(tz=tz))
            starttime = datetime.time(20, 0, 0, 0)
            endtime = datetime.time(23, 59, 0, 0)
            self.trigger = starttime < currtime < endtime

            if self.trigger:
                time.sleep(15)
                try:
                    # 미다운로드 샘플 확인 (path 가 NULL 이면 미다운로드로 간주)
                    sql = SELECT_SAMPLES_NOT_STORED
                    self.cur.execute(sql)
                except Exception as e:
                    self.logger.critical(str(e))
                    raise
                else:
                    if self.cur.rowcount:

                        # 미다운로드 md5 확보
                        md5s = [row[0].lower() for row in self.cur.fetchall()]

                        # 각각 다운로드
                        for md5 in md5s:
                            self.logger.info('downloading %s' % md5)

                            try:
                                content = self.download(md5)
                            except FileNotFoundError:
                                self.logger.info('no sample in virustotal %s' % md5)
                                continue
                            except PermissionError:
                                self.logger.info('reached daily api limit')
                                raise

                            # 다운로드 값 검증
                            m = hashlib.md5()
                            m.update(content)
                            if md5 != m.hexdigest():
                                self.logger.critical('download content differs from md5 value in report')
                                continue

                            # 서버저장 및 서버 저장위치 리턴
                            path = self.__store_sftp(md5, content)
                            if path:
                                path = path.replace('\\', '/')  # 저장경로 linux_path 로 변환
                            else:
                                self.logger.critical('failed to store SFTP')
                                raise IOError

                            try:
                                self.cur.execute(UPDATE_PATH, (path, md5))  # DB에 저장경로 업데이트
                                self.conn.commit()
                            except Exception as e:
                                self.logger.critical(str(e))
                                raise

    def download(self, md5):
        url = 'https://www.virustotal.com/vtapi/v2/file/download'

        # 파라미터를 설정한다
        params = {'apikey': self.api,
                   'hash': md5}

        # 다운로드 한다
        res = requests.get(url, params=params)

        # 응답코드를 검증한다
        if res.status_code == 404:
            raise FileNotFoundError
        elif res.status_code == 204:
            raise PermissionError

        return res.content


def setup_log(config):
    logger = logging.getLogger(config['logname'])
    logger.setLevel(config['loglevel'])
    filehandler = RotatingFileHandler(
        config['filename'],
        mode='a',
        maxBytes=config['maxsize'],
        backupCount=10
    )
    format = logging.Formatter(config['format'])
    filehandler.setFormatter(format)
    logger.addHandler(filehandler)

    return logger


if __name__ == '__main__':
    try:
        with open("config_master.yml", 'r') as ymlfile:
            config = yaml.load(ymlfile)
    except Exception:
        with open("config.yml", 'r') as ymlfile:
            config = yaml.load(ymlfile)

    setup_log(config['log'])

    while True:
        try:
            # 예외시 재접속
            vt = VTDownloader(config)
        except Exception as e:
            time.sleep(10)
            continue
        else:
            try:
                # 작업시작
                vt.work()
            except Exception as e:
                time.sleep(10)
                continue
