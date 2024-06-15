import copy
import datetime
import imaplib
import logging
import os
import signal
import socket
from enum import Enum
from typing import List, Optional
from getpass import getpass

from imap_tools import MailBox, AND

from src.config import Config, ConfigKey
from src.mail_message_ext import MailMessageExt
from src.message_exception import MessageException
from src.naming_utils import NamingUtils
from src.time_utils import parse_time

_logger = logging.getLogger(__name__)


class ExistsMethod(Enum):
    COMPARE = "compare"
    OVERWRITE = "overwrite"
    SKIP = "skip"

    def __str__(self):
        return self.__repr__()

    def __repr__(self) -> str:
        return '{}'.format(self.name)

class Runner:

    DEFAULT_EXIST_METHODE = ExistsMethod.COMPARE
    DEFAULT_FILE_PATTERN = "./downloads/{YEAR}-{MONTH}/{YEAR}{MONTH}{DAY}-{HOUR}{MINUTE}-{UID}-{SUBJECT}.eml"

    def __init__(self, config):
        self._config = config
        self._shutdown = False
        self._count_found = 0
        self._count_saved = 0
        self._count_skipped = 0

        signal.signal(signal.SIGINT, self._shutdown_gracefully)
        signal.signal(signal.SIGTERM, self._shutdown_gracefully)

        if _logger.isEnabledFor(logging.DEBUG):
            cloned_config = copy.deepcopy(self._config)
            cloned_config[ConfigKey.IMAP_PASSWORD.value] = "***"
            _logger.debug("config = %s", cloned_config)

        self._host = Config.get_str(self._config, ConfigKey.IMAP_HOST)
        self._port = Config.get_int(self._config, ConfigKey.IMAP_PORT)
        self._host_info = "{}:{}".format(self._host, self._port) if self._port else self._host

        self._pivot_path = config[ConfigKey.PIVOT_PATH.value]

    def _shutdown_gracefully(self, sig, _frame):
        _logger.info("shutdown signaled (%s)", sig)
        self._shutdown = True

    def run(self):
        try:
            self._connect()
        except socket.gaierror as ex:
            message = "Host ({}) not found! Error: {} ".format(self._host_info, ex.strerror)
            raise MessageException(message)
        except imaplib.IMAP4.error as ex:
            raise MessageException(str(ex))

    def _connect(self):
        username = Config.get_str(self._config, ConfigKey.IMAP_USERNAME)
        password = Config.get_str(self._config, ConfigKey.IMAP_PASSWORD)
        if not password:
            password = getpass(f"Enter you IMAP Password for {username}: ")
        if not self._host or not username or not password:
            raise MessageException("empty IMAP credentials!")

        MailBox.email_message_class = MailMessageExt

        kwargs = {"host": self._host}
        if self._port:
            kwargs["port"] = self._port

        delete = Config.get_bool(self._config, ConfigKey.DELETE)
        if delete:
            answer = input("Are you sure you want to delete downloaded messages on the server? (y/n)")
            if answer.lower() not in ["y","yes"]:
                _logger.info("Please check your mail-backup.yaml and run again.")
                return False

        with MailBox(**kwargs).login(username, password) as mailbox:
            _logger.info("logged in (%s@%s)", username, self._host_info)

            folders = mailbox.folder.list()
            folders_names = [f.name for f in folders]
            _logger.info("found mail folders = %s", folders_names)

            for folder in folders:
                if self._shutdown:
                    break

                mailbox.folder.set(folder.name)

                last_days = Config.get_int(self._config, ConfigKey.LAST_DAYS)
                limit_days = Config.get_int(self._config, ConfigKey.LIMIT_DAYS)
                since = None
                to = None
                if last_days and last_days > 0:
                    since = datetime.date.today() - datetime.timedelta(days=last_days)
                if limit_days and limit_days > 0:
                    to = datetime.date.today() - datetime.timedelta(days=limit_days)
                query_args = [AND(date_gte=since, date_lt=to)] if since or to else []
                
                try:
                    for mail in mailbox.fetch(*query_args, mark_seen=False):
                        self.handle_mail(mail, self._config, folder.name)
                        if delete:
                            mailbox.delete([mail.uid])

                        if self._shutdown:
                            break
                except Exception as ex:
                    _logger.error("error in folder: %s", folder.name)
                    raise ex

        _logger.info("success: %s mails saved (of %s found; %s skipped for legal reasons, e.g. already exists).",
                     self._count_saved, self._count_found, self._count_skipped)

    def handle_mail(self, mail: MailMessageExt, config: Config, folder: str = ""):
        attributes = NamingUtils.extract_attributes(mail, Config.get_str(self._config, ConfigKey.IMAP_USERNAME), folder)
        mail_path = NamingUtils.format_path(Config.get_str(self._config, ConfigKey.PATH), attributes)
        mail_path = os.path.realpath(NamingUtils.join_path(self._pivot_path, mail_path))
        folder_info = "folder '{}' - ".format(folder)

        self._count_found += 1

        mail_exists = os.path.isfile(mail_path)
        do_write = not mail_exists

        if not do_write:  # == mail file exists
            when_exists = Config.get_str(self._config, ConfigKey.WHEN_EXISTS)
            if when_exists == ExistsMethod.OVERWRITE:
                os.remove(mail_path)
                _logger.info("%sremove former mail (%s).", folder_info, mail_path)
                do_write = True
            elif when_exists == ExistsMethod.SKIP:
                _logger.debug("%sskip existing file (%s).", folder_info, mail_path)
                self._count_skipped += 1
            else:  # when_exists == ExistsMethod.COMPARE:
                new_mail_path = self.find_existing_file_or_new_mail_path(mail, mail_path, config)
                if new_mail_path:
                    mail_path = new_mail_path
                    do_write = True
                else:
                    self._count_skipped += 1

        if do_write:
            _logger.debug("%sbackup mail (%s).", folder_info, mail_path)
            mail_dir = os.path.dirname(mail_path)
            os.makedirs(mail_dir, exist_ok=True)
            with open(mail_path, "wb") as file:
                file.write(mail.raw_data)

            # Add file's creation date
            timestamp = parse_time(mail.date).strftime("%Y%m%d%H%M")
            now = datetime.datetime.now().strftime("%Y%m%d%H%M")
            os.system(f"touch -t {timestamp} '{mail_path}'")
            os.system(f"touch -m {now} '{mail_path}'")

            self._count_saved += 1

    @classmethod
    def find_existing_file_or_new_mail_path(cls, mail, orig_mail_path, config: Config = None, folder: str = "") -> Optional[str]:
        """
        :param MailMessageExt mail:
        :param str orig_mail_path:
        :param Optional[str] folder: only for logging folder info
        :return: new path to write or None when should not be written
        """
        if not os.path.isfile(orig_mail_path):
            return orig_mail_path

        folder_info = ""
        if config:
            folder_info = "folder '{}' - ".format(folder)

        loop = 0

        while loop < 5:
            if loop == 0:
                new_mail_path = orig_mail_path
            else:
                file_path, file_extension = os.path.splitext(orig_mail_path)
                new_mail_path = file_path + "." + str(loop + 1) + file_extension

            if not os.path.isfile(new_mail_path):
                return new_mail_path

            with open(new_mail_path, "rb") as file:
                compare_data = bytearray(file.read())

            if compare_data == mail.raw_data:
                if orig_mail_path == new_mail_path:
                    _logger.debug("%sskip existing mail (%s).", folder_info, orig_mail_path)
                else:
                    _logger.debug("%sskip existing mail (expected: %s, found as: %s).",
                                  folder_info, orig_mail_path, new_mail_path)
                return None

            loop += 1

        _logger.warning("cannot find other path for existing mail (%s). loop (%s) exceeded!", orig_mail_path, loop)

        return None
