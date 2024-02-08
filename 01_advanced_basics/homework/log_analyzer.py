from collections import defaultdict
from datetime import datetime
import configparser
import argparse
import logging
import string
import typing
import gzip
import json
import sys
import re
import os


config = {
    "REPORT_SIZE": 1000,
    "REPORT_DIR": r".\reports",
    "LOG_DIR": r".\log",
    "FAIL_THRESHOLD": 0.9
}

config_mapping = {
    "REPORT_SIZE": int,
    "REPORT_DIR": str,
    "LOG_DIR": str,
    "FAIL_THRESHOLD": float,
    "LOG_FILE": str,
}

line_format = re.compile(r'(?P<remote_addr>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}) (?P<remote_user>-|\w+) {1,2}(?P<http_x_real_ip>-|\w+) '
                         r'\[(?P<time_local>\d{2}\/[a-z]{3}\/\d{4}:\d{2}:\d{2}:\d{2} (\+|\-)\d{4})\] '
                         r'(?P<request>(\"0\")|(\"(?P<method>GET|POST|HEAD|PUT|PATCH|OPTIONS) )(?P<url>.+) (http\/1\.[0|1]")) '
                         r'(?P<status>\d{3}) (?P<body_bytes_sent>\d+) (\"(?P<http_referer>(\-)|(.+))\") '
                         r'(\"(?P<other_info>.+)\") (?P<request_time>[\d\.]+)', re.IGNORECASE)


def configure_logging(cfg):
    logging_kwargs = {
        "format": "[%(asctime)s] %(levelname).1s %(message)s",
        "datefmt": "%Y.%m.%d %H:%M:%S",
        "level": logging.INFO,
    }
    if cfg.get("LOG_FILE", None):
        logging_kwargs["filename"] = cfg["LOG_FILE"]
    else:
        logging_kwargs["stream"] = sys.stdout
    logging.basicConfig(**logging_kwargs)


def log_error(func):
    def inner(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except BaseException as e:
            logging.exception(f"{e}; function: {func.__name__}", exc_info=True)
    return inner


class AlwaysSortedList(list):
    get_key: callable

    def __init__(self, key: callable = None):
        if key is None:
            self.get_key = lambda x: x
        else:
            self.get_key = key
        super().__init__()

    def append(self, item: int | float | dict) -> None:
        if len(self) == 0:
            self.insert(0, item)
            return
        left, right = 0, len(self)
        while right - left > 0:
            m = (left + right) // 2
            if self.get_key(item) < self.get_key(self[m]):
                right = m
            else:
                left = m + 1
        self.insert(right, item)


def get_last_log(log_dir: str) -> [str, datetime]:
    if not os.path.exists(log_dir):
        raise FileNotFoundError("LOG_DIR does not exist")
    filename = ""
    last_date: datetime | None = None
    for _, _, files in os.walk(log_dir):
        for file in files:
            if not re.fullmatch(r"nginx-access-ui\.log-(\d+)(?:\.gz)?", file):
                continue
            m = re.search(r"nginx-access-ui\.log-(\d+)(?:\.gz)?", file)
            log_date = datetime.strptime(m.group(1), "%Y%m%d")
            if not last_date or (last_date and log_date > last_date):
                last_date = log_date
                filename = file
    return os.path.join(log_dir, filename), last_date


def log_reader(filename: str) -> typing.IO[bytes]:
    open_func = gzip.open if filename.endswith(".gz") else open
    return open_func(filename, "rb")


def parse_line(line: str) -> dict:
    m = re.search(line_format, line)
    return m.groupdict() if m else {}


def log_parser(filename: str):
    with log_reader(filename) as f:
        for line in f:
            yield parse_line(line.decode())


@log_error
def main(cfg):
    filename, log_date = get_last_log(cfg['LOG_DIR'])
    if not filename:
        return
    if not os.path.exists(cfg['REPORT_DIR']):
        os.mkdir(cfg['REPORT_DIR'])
    report_filepath = os.path.join(cfg['REPORT_DIR'], f'report-{log_date.strftime("%Y.%m.%d")}.html')
    if os.path.exists(report_filepath):
        return
    results = defaultdict(lambda: {"count": 0, "time_sum": 0., "time_avg": 0., "time_max": 0., "time": AlwaysSortedList()})
    total_time = 0.
    total_parsed_count = 0
    total_count = 0
    for result in log_parser(filename):
        total_count += 1
        if not result:
            continue
        total_parsed_count += 1
        obj = results[result['url']]
        req_time = float(result['request_time'])
        obj['count'] += 1
        obj['time_sum'] += req_time
        obj['time_max'] = max(obj['time_max'], req_time)
        obj['time'].append(req_time)
        total_time += req_time
    if total_count == 0:
        logging.error(f"File is empty: {filename}")
        return
    if (parsed_k := total_parsed_count / total_count) < cfg['FAIL_THRESHOLD']:
        logging.error(f"Too many lines weren`t parsed: {round((1 - parsed_k) * 100, 2)}%")
        return
    data = AlwaysSortedList(key=lambda x: -x['time_sum'])
    for url, obj in results.items():
        obj['time_sum'] = round(obj['time_sum'], 3)
        obj['time_max'] = round(obj['time_max'], 3)
        obj['time_avg'] = round(obj['time_sum'] / obj['count'], 3)
        obj['count_perc'] = round(obj['count'] / total_parsed_count * 100., 3)
        obj['time_perc'] = round(obj['time_sum'] / total_time * 100., 3)
        obj['time_med'] = obj['time'][obj['count'] // 2]
        obj['url'] = url
        del obj['time']
        data.append(obj)
    template = string.Template(open("report.html").read())
    with open(report_filepath, 'w', encoding='utf-8') as f:
        f.write(template.safe_substitute(table_json=json.dumps(data[:cfg['REPORT_SIZE']])))


if __name__ == "__main__":
    # Parse arguments
    parser = argparse.ArgumentParser(prog='Log Analyzer')
    parser.add_argument('--config', help='Config INI file')
    arguments = parser.parse_args()
    if arguments.config:
        if not os.path.exists(arguments.config):
            raise FileNotFoundError("Config file not found")
        conf_parser = configparser.ConfigParser()
        try:
            conf_parser.read(arguments.config)
        except configparser.ParsingError as e:
            raise ValueError(f"Invalid config file content: {e}")
        for k, v in conf_parser["Config"].items():
            k = k.upper()
            config[k] = config_mapping.get(k, str)(v)
    # Configure logging
    configure_logging(config)
    #
    main(config)
