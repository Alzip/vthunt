#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
main.py
_________
"""
import json
import time
import yaml
import requests
import pymysql
import logging
from logging.handlers import RotatingFileHandler


CHECK_NOTIFICATION_DUPLICATED = 'SELECT hunt FROM virustotal WHERE md5=%s'
INSERT_NOTIFICATION = 'INSERT INTO virustotal (hunt, md5) VALUES (%s, %s)'


class Notification:
    def __init__(self, config):
        self.config = config
        self.api = config['virustotal']['api']
        self.logger = logging.getLogger(config['log']['logname'])
        self.trigger = False
        self.conn = None
        self.cur = None
        self.url_noti = 'https://www.virustotal.com/intelligence/hunting/notifications-feed/?key=%s&output=json' % \
                        self.api
        self.url_del = 'https://www.virustotal.com/intelligence/hunting/delete-notifications/programmatic/?key=%s' % \
                       self.api
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



    def __delete_noti(self, ids):
        # virustotal 서버에서 noti 삭제
        try:
            requests.post(self.url_del, data=json.dumps(ids))
        except Exception:
            self.logger.critical('failed to delete notification')
            raise
        return True

    def work(self):
        while True:
            res = requests.post(self.url_noti)  # hunting notification 수령
            if res.content:
                content = res.json()
                notis = content['notifications']

                if notis:
                    self.logger.info('%d notifications received' % len(notis))

                    # 처리 완료된 notification id
                    complete_ids = list()

                    for noti in notis:  # 공지는 리스트형태
                        self.logger.info('%s in progress' % noti['md5'])

                        # 완료처리할 목록에 추가
                        complete_ids.append(noti['id'])

                        # 노티 중복확인
                        vals = (noti['md5'],)
                        self.cur.execute(CHECK_NOTIFICATION_DUPLICATED, vals)
                        if self.cur.rowcount:  # 중복, 패스
                            self.logger.info('already registered in samples.virustotal')
                        else:  # 신규등록해쉬
                            # DB 저장
                            vals = (json.dumps(noti), noti['md5'])
                            try:
                                self.cur.execute(INSERT_NOTIFICATION, vals)
                                self.conn.commit()
                            except Exception as e:
                                self.logger.critical('failed to insert new notification')
                                raise

                    # 완료처리 (virustotal 서버에서 noti 삭제)
                    self.__delete_noti(complete_ids)
            time.sleep(60)


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
    # 설정파일 열기
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
            vt = Notification(config)
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
