from __future__ import annotations

import gzip
import hashlib
import logging
import re
import string
import time
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Iterable, Iterator, TextIO, cast

import paramiko
from tqdm.autonotebook import tqdm

import pandas as pd
from pandas.errors import ParserError

from .api import HasBounds, OpenSkyDBAPI, ProgressbarType
from .config import (
    cache_path,
    impala_password,
    impala_username,
    ssh_proxycommand,
)
from .time import split_times, timelike, to_datetime

_log = logging.getLogger(__name__)


class ImpalaError(Exception):
    pass


@contextmanager
def open_cache_file(cachename: Path) -> Iterator[TextIO]:
    """Get a file object for the cache

    This abstracts away the compression status of the cache file.
    """
    with cachename.open("rb") as bytes_header:
        if bytes_header.read(3) == b"\x1f\x8b\x08":
            _log.info("Opening as Gzip {}".format(cachename))
            with gzip.open(cachename, "rt") as fh:
                yield fh
        else:
            _log.info("Opening as plain text {}".format(cachename))
            with cachename.open("r") as fh:
                yield fh


class Impala(OpenSkyDBAPI):
    """Wrapper to OpenSky Impala database

    Credentials are fetched from the configuration file.

    All methods return standard structures. When calls are made from the traffic
    library, they return advanced structures."""

    _impala_columns = (
        "time",
        "icao24",
        "lat",
        "lon",
        "velocity",
        "heading",
        "vertrate",
        "callsign",
        "onground",
        "alert",
        "spi",
        "squawk",
        "baroaltitude",
        "geoaltitude",
        "lastposupdate",
        "lastcontact",
        # "serials", keep commented, array<int>
        "hour",
    )

    _flarm_columns = (
        "sensortype",
        "sensorlatitude",
        "sensorlongitude",
        "sensoraltitude",
        "timeatserver",
        "timeatsensor",
        "timestamp",
        "timeatplane",
        "rawmessage",
        "crc",
        "rawsoftmessage",
        "sensorname",
        "ntperror",
        "userfreqcorrection",
        "autofreqcorrection",
        "frequency",
        "channel",
        "snrdetector",
        "snrdemodulator",
        "typeogn",
        "crccorrect",
        "hour",
    )

    _raw_tables = (
        "acas_data4",
        "allcall_replies_data4",
        "identification_data4",
        "operational_status_data4",
        "position_data4",
        "rollcall_replies_data4",
        "velocity_data4",
    )

    basic_request = (
        "select {columns} from state_vectors_data4 {other_tables} "
        "{where_clause} hour>={before_hour} and hour<{after_hour} "
        "and time>={before_time} and time<{after_time} "
        "{other_params}"
    )

    flarm_request = (
        "select * from flarm_raw "
        "where hour>={before_hour} and hour<{after_hour} "
        # "and timeatsensor>={before_time} and timeatsensor<{after_time} "
    )

    _parseErrorMsg = """
    Error at parsing the cache file, moved to a temporary directory: {path}.
    Running the request again may help.

    For more information, find below the error and the buggy line:
    """

    stdin: paramiko.ChannelFile  # type: ignore
    stdout: paramiko.ChannelFile  # type: ignore
    stderr: paramiko.ChannelFile  # type: ignore
    # actually ChannelStderrFile

    def __init__(self, **kwargs: Any) -> None:
        if impala_username is None or impala_password is None:
            _log.warn("No credentials provided")

        self.username = impala_username
        self.password = impala_password
        self.proxy_command = ssh_proxycommand
        self.connected = False
        self.cache_dir = cache_path
        if not self.cache_dir.exists():
            self.cache_dir.mkdir(parents=True)

        if impala_username == "" or impala_password == "":
            self.auth = None
        else:
            self.auth = (impala_username, impala_password)

    def clear_cache(self) -> None:  # coverage: ignore
        """Clear cache files for OpenSky.

        The directory containing cache files tends to clog after a while.
        """
        for file in self.cache_dir.glob("*"):
            file.unlink()

    @staticmethod
    def _read_cache(cachename: Path) -> None | pd.DataFrame:
        _log.info("Reading request in cache {}".format(cachename))
        with open_cache_file(cachename) as fh:
            s = StringIO()
            count = 0
            for line in fh.readlines():
                # -- no pretty-print style cache (option -B)
                if re.search("\t", line):
                    count += 1
                    s.write(re.sub(" *\t *", ",", line))
                    s.write("\n")
                # -- pretty-print style cache
                if re.match(r"\|.*\|", line):
                    count += 1
                    if "," in line:  # this may happen on 'describe table'
                        return_df = False
                        break
                    s.write(re.sub(r" *\| *", ",", line)[1:-2])
                    s.write("\n")
            else:
                return_df = True

            if not return_df:
                fh.seek(0)
                return "".join(fh.readlines())

            if count > 0:
                s.seek(0)
                try:
                    # otherwise pandas would parse 1234e5 as 123400000.0
                    df = pd.read_csv(s, dtype={"icao24": str, "callsign": str})
                except ParserError as error:
                    for x in re.finditer(r"line (\d)+,", error.args[0]):
                        line_nb = int(x.group(1))
                        with open_cache_file(cachename) as fh:
                            content = fh.readlines()[line_nb - 1]

                    new_path = Path(gettempdir()) / cachename.name
                    cachename.rename(new_path)
                    raise ImpalaError(
                        Impala._parseErrorMsg.format(path=new_path)
                        + (error + "\n" + content)
                    )

                if df.shape[0] > 0:
                    return df.drop_duplicates()

        error_msg: None | str = None
        with open_cache_file(cachename) as fh:
            output = fh.readlines()
            if any(elt.startswith("ERROR:") for elt in output):
                error_msg = "".join(output[:-1])

        if error_msg is not None:
            cachename.unlink()
            raise ImpalaError(error_msg)

        return None

    @staticmethod
    def _format_dataframe(
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        This function converts types, strips spaces after callsigns and sorts
        the DataFrame by timestamp.

        For some reason, all data arriving from OpenSky are converted to
        units in metric system. Optionally, you may convert the units back to
        nautical miles, feet and feet/min.

        """

        if "callsign" in df.columns and df.callsign.dtype == object:
            df.callsign = df.callsign.str.strip()

        df.icao24 = (
            df.icao24.apply(int, base=16)
            .apply(hex)
            .str.slice(2)
            .str.pad(6, fillchar="0")
        )

        if "rawmsg" in df.columns and df.rawmsg.dtype != str:
            df.rawmsg = df.rawmsg.astype(str).str.strip()

        if "squawk" in df.columns:
            df.squawk = (
                df.squawk.astype(str)
                .str.split(".")
                .str[0]
                .replace({"nan": None})
            )

        time_dict: dict[str, pd.Series] = dict()
        for colname in [
            "lastposupdate",
            "lastposition",
            "firstseen",
            "lastseen",
            "mintime",
            "maxtime",
            "time",
            "timestamp",
            "day",
            "hour",
        ]:
            if colname in df.columns:
                time_dict[colname] = pd.to_datetime(
                    df[colname] * 1e9
                ).dt.tz_localize("utc")

        return df.assign(**time_dict)

    def _connect(self) -> None:  # coverage: ignore
        if self.username == "" or self.password == "":
            raise RuntimeError("This method requires authentication.")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        extra_args = dict()

        if self.proxy_command is not None and self.proxy_command != "":
            # for instance:
            #    "ssh -W data.opensky-network.org:2230 proxy_machine"
            # or "connect.exe -H proxy_ip:proxy_port %h %p"
            _log.info(f"Using ProxyCommand: {self.proxy_command}")
            extra_args["sock"] = paramiko.ProxyCommand(self.proxy_command)

        client.connect(
            "data.opensky-network.org",
            port=2230,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            compress=True,
            **extra_args,  # type: ignore
        )
        self.stdin, self.stdout, self.stderr = client.exec_command(
            "-B", bufsize=-1, get_pty=True
        )
        self.connected = True
        total = ""
        while len(total) == 0 or total[-10:] != ":21000] > ":
            b = self.stdout.channel.recv(256)
            total += b.decode()

    def _impala(
        self,
        request: str,
        columns: str,
        cached: bool = True,
        compress: bool = False,
    ) -> None | pd.DataFrame:  # coverage: ignore
        digest = hashlib.md5(request.encode("utf8")).hexdigest()
        cachename = self.cache_dir / digest

        if cachename.exists() and not cached:
            cachename.unlink()

        if not cachename.exists():
            _log.info("Sending request: {}".format(request))

            if not self.connected:
                _log.info("Connecting the database")
                self._connect()

            # bug fix for when we write a request with """ starting with \n
            request = request.replace("\n", " ")
            _log.info(request)

            self.stdin.channel.send(request + ";\n")
            # avoid messing lines in the cache file
            time.sleep(0.1)
            total = ""
            _log.info("Will be writing into {}".format(cachename))
            while len(total) == 0 or total[-10:] != ":21000] > ":
                b = self.stdout.channel.recv(256)
                total += b.decode()
            # There is no direct streaming into the cache file.
            # The reason for that is the connection may stall, your computer
            # may crash or the programme may exit abruptly in spite of your
            # (and my) best efforts to handle exceptions.
            # If data is streamed directly into the cache file, it is hard to
            # detect that it is corrupted and should be removed/overwritten.
            _log.info("Opening {}".format(cachename))
            if compress:
                cache_file = gzip.open(cachename, "wt")
            else:
                cache_file = cachename.open("w")
            with cache_file as fh:
                if columns is not None:
                    fh.write(re.sub(", ", "\t", columns))
                    fh.write("\n")
                fh.write(total)
            _log.info("Closing {}".format(cachename))

        return self._read_cache(cachename)

    def request(
        self,
        request_pattern: str,
        start: timelike,
        stop: timelike,
        *args: Any,  # more reasonable to be explicit about arguments
        columns: list[str],
        date_delta: timedelta = timedelta(hours=1),
        cached: bool = True,
        compress: bool = False,
        progressbar: bool | ProgressbarType[Any] = True,
    ) -> pd.DataFrame:
        """Splits and sends a custom request.

        :param request_pattern: a string containing the basic request you
            wish to make on Impala shell. Use {before_hour} and {after_hour}
            place holders to write your hour constraints: they will be
            automatically replaced by appropriate values.
        :param start: a string (default to UTC), epoch or datetime (native
            Python or pandas)
        :param stop: a string (default to UTC), epoch or datetime
              (native Python or pandas), *by default, one day after start*
        :param columns: the list of expected columns in the result. This
              helps naming the columns in the resulting dataframe.
        :param date_delta: a timedelta representing how to split the requests,
            *by default: per hour*
        :param cached: (default: True) switch to False to force a new request to
            the database regardless of the cached files; delete previous cache
            files;
        :param compress: (default: False) compress cache files. Reduces disk
            space occupied at the expense of slightly increased time
            to load.

        """

        start_ts = to_datetime(start)
        stop_ts = (
            to_datetime(stop)
            if stop is not None
            else start_ts + pd.Timedelta("1d")
        )

        if progressbar is True:
            if stop_ts - start_ts > date_delta:
                progressbar = tqdm
            else:
                progressbar = iter

        if progressbar is False:
            progressbar = iter

        progressbar = cast(ProgressbarType[Any], progressbar)

        cumul: list[pd.DataFrame] = []
        sequence = list(split_times(start_ts, stop_ts, date_delta))

        for bt, at, bh, ah in progressbar(sequence):
            _log.info(
                f"Sending request between time {bt} and {at} "
                f"and hour {bh} and {ah}"
            )

            request = request_pattern.format(
                before_time=bt.timestamp(),
                after_time=at.timestamp(),
                before_hour=bh.timestamp(),
                after_hour=ah.timestamp(),
            )

            df = self._impala(
                request,
                columns="\t".join(columns),
                cached=cached,
                compress=compress,
            )

            if df is None:
                continue

            cumul.append(df)

        if len(cumul) == 0:
            return None

        return pd.concat(cumul)

    def flightlist(
        self,
        start: timelike,
        stop: None | timelike = None,
        *args: Any,  # more reasonable to be explicit about arguments
        departure_airport: None | str | list[str] = None,
        arrival_airport: None | str | list[str] = None,
        airport: None | str | list[str] = None,
        callsign: None | str | list[str] = None,
        icao24: None | str | list[str] = None,
        cached: bool = True,
        compress: bool = False,
        limit: None | int = None,
        progressbar: bool | ProgressbarType[Any] = True,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Lists flights departing or arriving at a given airport.

        You may pass requests based on time ranges, callsigns, aircraft, areas,
        serial numbers for receivers, or airports of departure or arrival.

        The method builds appropriate SQL requests, caches results and formats
        data into a proper pandas DataFrame. Requests are split by hour (by
        default) in case the connection fails.

        :param start: a string (default to UTC), epoch or datetime (native
            Python or pandas)
        :param stop: a string (default to UTC), epoch or datetime (native Python
            or pandas), *by default, one day after start*

        More arguments to filter resulting data:

        :param departure_airport: a string for the ICAO identifier of the
            airport. Selects flights departing from the airport between the two
            timestamps;
        :param arrival_airport: a string for the ICAO identifier of the airport.
            Selects flights arriving at the airport between the two timestamps;
        :param airport: a string for the ICAO identifier of the airport. Selects
            flights departing from or arriving at the airport between the two
            timestamps;
        :param callsign: a string or a list of strings (wildcards
            accepted, _ for any character, % for any sequence of characters);
        :param icao24: a string or a list of strings identifying the transponder
            code of the aircraft;

        .. warning::

            - If both departure_airport and arrival_airport are set, requested
              timestamps match the arrival time;
            - If airport is set, ``departure_airport`` and ``arrival_airport``
              cannot be specified (a RuntimeException is raised).

        **Useful options for debug**

        :param cached: (default: True) switch to False to force a new request to
            the database regardless of the cached files. This option also
            deletes previous cache files;
        :param compress: (default: False) compress cache files. Reduces disk
            space occupied at the expense of slightly increased time
            to load.
        :param limit: maximum number of records requested, LIMIT keyword in SQL.

        """

        query_str = (
            "select {columns} from flights_data4 "
            "where day >= {before_day} and day < {after_day} "
            "{other_params}"
        )
        columns = ", ".join(
            [
                "icao24",
                "firstseen",
                "estdepartureairport",
                "lastseen",
                "estarrivalairport",
                "callsign",
                "day",
            ]
        )

        start_ts = to_datetime(start)
        stop_ts = (
            to_datetime(stop)
            if stop is not None
            else start_ts + pd.Timedelta("1d")
        )

        if progressbar is True:
            if stop_ts - start_ts > timedelta(days=1):
                progressbar = tqdm
            else:
                progressbar = iter

        if progressbar is False:
            progressbar = iter

        progressbar = cast(ProgressbarType[Any], progressbar)

        other_params = ""

        if isinstance(icao24, str):
            other_params += "and icao24='{}' ".format(icao24.lower())

        elif isinstance(icao24, Iterable):
            icao24 = ",".join("'{}'".format(c.lower()) for c in icao24)
            other_params += "and icao24 in ({}) ".format(icao24)

        if isinstance(callsign, str):
            if callsign.find("%") > 0 or callsign.find("_") > 0:
                other_params += "and callsign ilike '{}' ".format(callsign)
            else:
                other_params += "and callsign='{:<8s}' ".format(callsign)

        elif isinstance(callsign, Iterable):
            callsign = ",".join("'{:<8s}'".format(c) for c in callsign)
            other_params += "and callsign in ({}) ".format(callsign)

        if departure_airport is not None:
            other_params += (
                f"and firstseen >= {start_ts.timestamp()} and "
                f"firstseen < {stop_ts.timestamp()} "
            )
        else:
            other_params += (
                f"and lastseen >= {start_ts.timestamp()} and "
                f"lastseen < {stop_ts.timestamp()} "
            )

        if airport:
            other_params += (
                f"and (estarrivalairport = '{airport}' or "
                f"estdepartureairport = '{airport}') "
            )
            if departure_airport is not None or arrival_airport is not None:
                raise RuntimeError(
                    "airport may not be set if "
                    "either arrival_airport or departure_airport is set"
                )
        else:
            if departure_airport:
                other_params += (
                    f"and estdepartureairport = '{departure_airport}' "
                )
            if arrival_airport:
                other_params += f"and estarrivalairport = '{arrival_airport}' "

        cumul = []
        sequence = list(split_times(start_ts, stop_ts, timedelta(days=1)))

        if limit is not None:
            other_params += f"limit {limit}"

        for bt, at, before_day, after_day in progressbar(sequence):
            _log.info(
                f"Sending request between time {bt} and {at} "
                f"and day {before_day} and {after_day}"
            )

            request = query_str.format(
                columns=columns,
                before_day=before_day.timestamp(),
                after_day=after_day.timestamp(),
                other_params=other_params,
            )

            df = self._impala(
                request, columns=columns, cached=cached, compress=compress
            )

            if df is None:
                continue

            df = self._format_dataframe(df)

            cumul.append(df)

        if len(cumul) == 0:
            return None

        df = pd.concat(cumul).rename(
            columns=dict(
                estarrivalairport="arrival",
                estdepartureairport="departure",
            )
        )

        return df

    def history(
        self,
        start: timelike,
        stop: None | timelike = None,
        *args: Any,  # more reasonable to be explicit about arguments
        callsign: None | str | list[str] = None,
        icao24: None | str | list[str] = None,
        serials: None | int | Iterable[int] = None,
        bounds: None
        | str
        | HasBounds
        | tuple[float, float, float, float] = None,
        departure_airport: None | str = None,
        arrival_airport: None | str = None,
        airport: None | str = None,
        time_buffer: None | str | pd.Timedelta = None,
        cached: bool = True,
        compress: bool = False,
        limit: None | int = None,
        other_tables: str = "",
        other_params: str = "",
        progressbar: bool | ProgressbarType[Any] = True,
        date_delta: timedelta = timedelta(hours=1),
        count: bool = False,
        **kwargs: Any,
    ) -> None | pd.DataFrame:
        """Get Traffic from the OpenSky Impala shell.

        You may pass requests based on time ranges, callsigns, aircraft, areas,
        serial numbers for receivers, or airports of departure or arrival.

        The method builds appropriate SQL requests, caches results and formats
        data into a proper pandas DataFrame. Requests are split by hour (by
        default) in case the connection fails.

        :param start: a string (default to UTC), epoch or datetime (native
            Python or pandas)
        :param stop: a string (default to UTC), epoch or datetime (native Python
            or pandas), *by default, one day after start*
        :param date_delta: a timedelta representing how to split the requests,
            *by default: per hour*
        :param return_flight: returns a Flight instead of a Traffic structure if
            switched to True

        More arguments to filter resulting data:

        :param callsign: a string or a list of strings (wildcards
            accepted, _ for any character, % for any sequence of characters);
        :param icao24: a string or a list of strings identifying the transponder
            code of the aircraft;
        :param serials: an integer or a list of integers identifying the sensors
            receiving the data;
        :param bounds: sets a geographical footprint. Either an **airspace or
            shapely shape** (requires the bounds attribute); or a **tuple of
            float** (west, south, east, north);

        **Airports**

        The following options build more complicated requests by merging
        information from two tables in the Impala database, resp.
        ``state_vectors_data4`` and ``flights_data4``.

        :param departure_airport: a string for the ICAO identifier of the
            airport. Selects flights departing from the airport between the two
            timestamps;
        :param arrival_airport: a string for the ICAO identifier of the airport.
            Selects flights arriving at the airport between the two timestamps;
        :param airport: a string for the ICAO identifier of the airport. Selects
            flights departing from or arriving at the airport between the two
            timestamps;
        :param time_buffer: (default: None) time buffer used to extend time
            bounds for flights in the OpenSky flight tables: requests will get
            flights between ``start - time_buffer`` and ``stop + time_buffer``.
            If no airport is specified, the parameter is ignored.

        .. warning::

            - See `opensky.flightlist
              <#traffic.data.adsb.opensky_impala.Impala.flightlist>`__ if you do
              not need any trajectory information.
            - If both departure_airport and arrival_airport are set, requested
              timestamps match the arrival time;
            - If airport is set, departure_airport and arrival_airport cannot be
              specified (a RuntimeException is raised).

        **Useful options for debug**

        :param count: (default: False) add a column stating how many sensors
            received each record;
        :param nautical_units: (default: True) convert data stored in Impala to
            standard nautical units (ft, ft/min, knots).
        :param cached: (default: True) switch to False to force a new request to
            the database regardless of the cached files. This option also
            deletes previous cache files;
        :param compress: (default: False) compress cache files. Reduces disk
            space occupied at the expense of slightly increased time
            to load.
        :param limit: maximum number of records requested, LIMIT keyword in SQL.

        """

        start_ts = to_datetime(start)
        stop_ts = (
            to_datetime(stop)
            if stop is not None
            else start_ts + pd.Timedelta("1d")
        )

        regexp_in_callsign = False

        # default obvious parameter
        where_clause = "where"

        if progressbar is True:
            if stop_ts - start_ts > date_delta:
                progressbar = tqdm
            else:
                progressbar = iter

        if progressbar is True:
            if stop_ts - start_ts > date_delta:
                progressbar = tqdm
            else:
                progressbar = iter

        if progressbar is False:
            progressbar = iter

        progressbar = cast(ProgressbarType[Any], progressbar)

        airports_params = [airport, departure_airport, arrival_airport]
        count_airports_params = sum(x is not None for x in airports_params)

        if count is True and serials is None:
            other_tables += ", state_vectors_data4.serials s "

        if isinstance(serials, Iterable):
            other_tables += ", state_vectors_data4.serials s "
            other_params += "and s.ITEM in {} ".format(tuple(serials))
        elif isinstance(serials, int):
            other_tables += ", state_vectors_data4.serials s "
            other_params += "and s.ITEM = {} ".format(serials)

        if isinstance(icao24, str):
            other_params += "and icao24='{}' ".format(icao24.lower())

        elif isinstance(icao24, Iterable):
            icao24 = ",".join("'{}'".format(c.lower()) for c in icao24)
            other_params += "and icao24 in ({}) ".format(icao24)

        if isinstance(callsign, str):
            if (
                set(callsign)
                - set(string.ascii_letters)
                - set(string.digits)
                - set("%_")
            ):  # if regex like characters
                regexp_in_callsign = True
                if callsign.find("REGEXP("):  # useful for NOT REGEXP()
                    other_params += f"and RTRIM(callsign) {callsign} "
                else:
                    other_params += f"and RTRIM(callsign) REGEXP('{callsign}') "

            elif callsign.find("%") >= 0 or callsign.find("_") >= 0:
                other_params += "and callsign ilike '{}' ".format(callsign)
            else:
                other_params += "and callsign='{:<8s}' ".format(callsign)

        elif isinstance(callsign, Iterable):
            callsign = ",".join("'{:<8s}'".format(c) for c in callsign)
            other_params += "and callsign in ({}) ".format(callsign)

        if bounds is not None:
            if isinstance(bounds, str):
                from cartes.osm import Nominatim

                bounds = cast(HasBounds, Nominatim.search(bounds))
                if bounds is None:
                    raise RuntimeError(f"'{bounds}' not found on Nominatim")

            if hasattr(bounds, "bounds"):
                # thinking of shapely bounds attribute (in this order)
                # I just don't want to add the shapely dependency here
                west, south, east, north = getattr(bounds, "bounds")
            else:
                west, south, east, north = bounds

            other_params += "and lon>={} and lon<={} ".format(west, east)
            other_params += "and lat>={} and lat<={} ".format(south, north)

        day_min = start_ts.floor("1d")
        day_max = stop_ts.ceil("1d")

        if count_airports_params > 0:
            if isinstance(time_buffer, str):
                time_buffer = pd.Timedelta(time_buffer)
            buffer_s = time_buffer.total_seconds() if time_buffer else 0
            where_clause = (
                "on icao24 = est.e_icao24 and "
                "callsign = est.e_callsign and "
                f"est.firstseen - {buffer_s} <= time and "
                f"time <= est.lastseen + {buffer_s} "
                "where"
            )

        if arrival_airport is not None and departure_airport is not None:
            if airport is not None:
                raise RuntimeError(
                    "airport may not be set if "
                    "either arrival_airport or departure_airport is set"
                )
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign as e_callsign, day from flights_data4 "
                "where estdepartureairport ='{departure_airport}' "
                "and estarrivalairport ='{arrival_airport}' "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                arrival_airport=arrival_airport,
                departure_airport=departure_airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        elif arrival_airport is not None:
            if airport is not None:
                raise RuntimeError(
                    "airport may not be set if " "arrival_airport is set"
                )
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign as e_callsign, day from flights_data4 "
                "where estarrivalairport ='{arrival_airport}' "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                arrival_airport=arrival_airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        elif departure_airport is not None:
            if airport is not None:
                raise RuntimeError(
                    "airport may not be set if " "departure_airport is set"
                )
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign as e_callsign, day from flights_data4 "
                "where estdepartureairport ='{departure_airport}' "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                departure_airport=departure_airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        elif airport is not None:
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign as e_callsign, day from flights_data4 "
                "where (estdepartureairport ='{arrival_or_departure_airport}' "
                "or estarrivalairport = '{arrival_or_departure_airport}') "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                arrival_or_departure_airport=airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        cumul: list[pd.DataFrame] = []
        sequence = list(split_times(start_ts, stop_ts, date_delta))
        columns = ", ".join(f"{field}" for field in self._impala_columns)
        parse_columns = ", ".join(self._impala_columns)

        if count_airports_params > 0:
            est_columns = [
                "firstseen",
                "estdepartureairport",
                "lastseen",
                "estarrivalairport",
                "day",
            ]
            columns = columns + (
                ", " + ", ".join(f"est.{field}" for field in est_columns)
            )
            parse_columns = ", ".join(
                [
                    *self._impala_columns,
                    "firstseen",
                    "origin",
                    "lastseen",
                    "destination",
                    "day",
                ]
            )

        if count is True:
            other_params += "group by " + columns + " "
            columns = "count(*) as count, " + columns
            parse_columns = "count, " + parse_columns

        if limit is not None:
            other_params += f"limit {limit}"

        for bt, at, bh, ah in progressbar(sequence):
            _log.info(
                f"Sending request between time {bt} and {at} "
                f"and hour {bh} and {ah}"
            )

            request = self.basic_request.format(
                columns=columns,
                before_time=bt.timestamp(),
                after_time=at.timestamp(),
                before_hour=bh.timestamp(),
                after_hour=ah.timestamp(),
                other_tables=other_tables,
                other_params=other_params
                if regexp_in_callsign  # TODO temporary ugly fix
                else other_params.format(
                    before_time=bt.timestamp(),
                    after_time=at.timestamp(),
                    before_hour=bh.timestamp(),
                    after_hour=ah.timestamp(),
                ),
                where_clause=where_clause,
            )

            df = self._impala(
                request, columns=parse_columns, cached=cached, compress=compress
            )

            if df is None:
                continue

            df = self._format_dataframe(df)

            cumul.append(df)

        if len(cumul) == 0:
            return None

        df = pd.concat(cumul)  # .sort_values("time")

        if count is True:
            df = df.assign(count=lambda df: df["count"].astype(int))

        return df

    def flarm(
        self,
        start: timelike,
        stop: None | timelike = None,
        *args: Any,  # more reasonable to be explicit about arguments
        sensor_name: None | str | list[str] = None,
        cached: bool = True,
        compress: bool = False,
        limit: None | int = None,
        other_params: str = "",
        progressbar: bool | ProgressbarType[Any] = True,
    ) -> None | pd.DataFrame:
        other_params += "and rawmessage = rawmessage and crccorrect "
        other_params += "and not typeogn "

        if isinstance(sensor_name, str):
            if sensor_name.find("%") > -1 or sensor_name.find("_") > -1:
                other_params += "and sensorname ilike '{}' ".format(sensor_name)
            else:
                other_params += "and sensorname='{:<8s}' ".format(sensor_name)

        elif isinstance(sensor_name, Iterable):
            sensor_name = ", ".join(sensor_name)
            other_params += "and sensorname in ({}) ".format(sensor_name)

        if limit is not None:
            other_params += f"limit {limit}"

        pattern = self.flarm_request + other_params

        data = self.request(
            request_pattern=pattern,
            start=start,
            stop=stop,
            columns=list(self._flarm_columns),
            cached=cached,
            compress=compress,
            progressbar=progressbar,
        )

        if data is None:
            return None

        return data

    def rawdata(
        self,
        start: timelike,
        stop: None | timelike = None,
        *args: Any,  # more reasonable to be explicit about arguments
        icao24: None | str | list[str] = None,
        serials: None | int | Iterable[int] = None,
        bounds: None | HasBounds | tuple[float, float, float, float] = None,
        callsign: None | str | list[str] = None,
        departure_airport: None | str = None,
        arrival_airport: None | str = None,
        airport: None | str = None,
        cached: bool = True,
        compress: bool = False,
        limit: None | int = None,
        date_delta: timedelta = timedelta(hours=1),
        table_name: None | str | list[str] = None,
        other_tables: str = "",
        other_columns: None | str | list[str] = None,
        other_params: str = "",
        progressbar: bool | ProgressbarType[Any] = True,
        **kwargs: Any,
    ) -> None | pd.DataFrame:
        """Get raw message from the OpenSky Impala shell.

        You may pass requests based on time ranges, callsigns, aircraft, areas,
        serial numbers for receivers, or airports of departure or arrival.

        The method builds appropriate SQL requests, caches results and formats
        data into a proper pandas DataFrame. Requests are split by hour (by
        default) in case the connection fails.


        :param start: a string (default to UTC), epoch or datetime (native
            Python or pandas)
        :param stop: a string (default to UTC), epoch or datetime (native Python
            or pandas), *by default, one day after start*
        :param table_name: one or several of Impala tables (listed in
            `opensky._raw_tables`)
        :param date_delta: a timedelta representing how to split the requests,
            *by default: per hour*

        More arguments to filter resulting data:

        :param callsign: a string or a list of strings (wildcards
            accepted, _ for any character, % for any sequence of characters);
        :param icao24: a string or a list of strings identifying the transponder
            code of the aircraft;
        :param serials: an integer or a list of integers identifying the sensors
            receiving the data;
        :param bounds: sets a geographical footprint. Either an **airspace or
            shapely shape** (requires the bounds attribute); or a **tuple of
            float** (west, south, east, north);

        **Airports**

        The following options build more complicated requests by merging
        information from two tables in the Impala database, resp.
        ``rollcall_replies_data4`` and ``flights_data4``.

        :param departure_airport: a string for the ICAO identifier of the
            airport. Selects flights departing from the airport between the two
            timestamps;
        :param arrival_airport: a string for the ICAO identifier of the airport.
            Selects flights arriving at the airport between the two timestamps;
        :param airport: a string for the ICAO identifier of the airport. Selects
            flights departing from or arriving at the airport between the two
            timestamps;

        .. warning::

            - If both departure_airport and arrival_airport are set, requested
              timestamps match the arrival time;
            - If airport is set, departure_airport and arrival_airport cannot be
              specified (a RuntimeException is raised).
            - It is not possible at the moment to filter both on airports and on
              geographical bounds (help welcome!).

        **Useful options for debug**

        :param cached: (default: True) switch to False to force a new request to
            the database regardless of the cached files. This option also
            deletes previous cache files;
        :param compress: (default: False) compress cache files. Reduces disk
            space occupied at the expense of slightly increased time
            to load.
        :param limit: maximum number of records requested, LIMIT keyword in SQL.

        """

        if table_name is None:
            table_name = list(self._raw_tables)

        if not isinstance(table_name, str):  # better than Iterable but not str
            pieces = list(
                self.rawdata(
                    start,
                    stop,
                    table_name=table,
                    date_delta=date_delta,
                    icao24=icao24,
                    serials=serials,
                    bounds=bounds,
                    callsign=callsign,
                    departure_airport=departure_airport,
                    arrival_airport=arrival_airport,
                    airport=airport,
                    cached=cached,
                    limit=limit,
                    other_tables=other_tables,
                    other_columns=other_columns,
                    other_params=other_params,
                    progressbar=progressbar,
                )
                for table in table_name
            )
            candidates = [df for df in pieces if df is not None]
            return pd.concat(candidates)

        _request = (
            "select {columns} from {table_name} {other_tables} "
            "{where_clause} hour>={before_hour} and hour<{after_hour} "
            "and {table_name}.mintime>={before_time} and "
            "{table_name}.mintime<{after_time} "
            "{other_params}"
        )

        columns = "mintime, maxtime, rawmsg, msgcount, icao24, hour"
        if other_columns is not None:
            if isinstance(other_columns, str):
                columns += f", {other_columns}"
            else:
                columns += ", " + ", ".join(other_columns)
        parse_columns = columns

        # default obvious parameter
        where_clause = "where"

        airports_params = [airport, departure_airport, arrival_airport]
        count_airports_params = sum(x is not None for x in airports_params)

        if table_name not in self._raw_tables:
            raise RuntimeError(f"{table_name} is not a valid table name")

        start_ts = to_datetime(start)
        stop_ts = (
            to_datetime(stop)
            if stop is not None
            else start_ts + pd.Timedelta("1d")
        )

        if progressbar is True:
            if stop_ts - start_ts > date_delta:
                progressbar = tqdm
            else:
                progressbar = iter

        if progressbar is False:
            progressbar = iter

        progressbar = cast(ProgressbarType[Any], progressbar)

        if isinstance(icao24, str):
            other_params += f"and {table_name}.icao24='{icao24.lower()}' "
        elif isinstance(icao24, Iterable):
            icao24 = ",".join("'{}'".format(c.lower()) for c in icao24)
            other_params += f"and {table_name}.icao24 in ({icao24}) "

        if isinstance(serials, Iterable):
            other_tables += f", {table_name}.sensors s "
            other_params += "and s.serial in {} ".format(tuple(serials))
            columns = "s.serial, s.mintime as time, " + columns
            parse_columns = "serial, time, " + parse_columns
        elif isinstance(serials, int):
            other_tables += f", {table_name}.sensors s "
            other_params += "and s.serial = {} ".format((serials))
            columns = "s.serial, s.mintime as time, " + columns
            parse_columns = "serial, time, " + parse_columns

        other_params += "and rawmsg is not null "

        day_min = start_ts.floor("1d")
        day_max = stop_ts.ceil("1d")

        if (
            count_airports_params > 0
            or bounds is not None
            or callsign is not None
        ):
            where_clause = (
                f"on {table_name}.icao24 = est.e_icao24 and "
                f"est.firstseen <= {table_name}.mintime and "
                f"{table_name}.mintime <= est.lastseen "
                "where"
            )
        if callsign is not None:
            if count_airports_params > 0 or bounds is not None:
                raise RuntimeError(
                    "Either callsign, bounds or airport are "
                    "supported at the moment."
                )
            if isinstance(callsign, str):
                if callsign.find("%") > -1 or callsign.find("_") > -1:
                    callsigns = "and callsign ilike '{}' ".format(callsign)
                else:
                    callsigns = "and callsign='{:<8s}' ".format(callsign)

            elif isinstance(callsign, Iterable):
                callsign = ",".join("'{:<8s}'".format(c) for c in callsign)
                callsigns = "and callsign in ({}) ".format(callsign)

            other_tables += (
                "join (select min(time) as firstseen, max(time) as lastseen, "
                "icao24  as e_icao24 from state_vectors_data4 "
                "where hour>={before_hour} and hour<{after_hour} and "
                f"time>={start_ts.timestamp()} and time<{stop_ts.timestamp()} "
                f"{callsigns}"
                "group by icao24) as est "
            )

        elif bounds is not None:
            if count_airports_params > 0:
                raise RuntimeError(
                    "Either bounds or airport are supported at the moment."
                )
            if hasattr(bounds, "bounds"):
                # thinking of shapely bounds attribute (in this order)
                # I just don't want to add the shapely dependency here
                west, south, east, north = getattr(bounds, "bounds")
            else:
                west, south, east, north = bounds

            other_tables += (
                "join (select min(time) as firstseen, max(time) as lastseen, "
                "icao24 as e_icao24 from state_vectors_data4 "
                "where hour>={before_hour} and hour<{after_hour} and "
                f"time>={start_ts.timestamp()} and time<{stop_ts.timestamp()} "
                f"and lon>={west} and lon<={east} "
                f"and lat>={south} and lat<={north} "
                "group by icao24) as est "
            )

        elif arrival_airport is not None and departure_airport is not None:
            if airport is not None:
                raise RuntimeError(
                    "airport may not be set if "
                    "either arrival_airport or departure_airport is set"
                )
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign, day from flights_data4 "
                "where estdepartureairport ='{departure_airport}' "
                "and estarrivalairport ='{arrival_airport}' "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                arrival_airport=arrival_airport,
                departure_airport=departure_airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        elif arrival_airport is not None:
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign, day from flights_data4 "
                "where estarrivalairport ='{arrival_airport}' "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                arrival_airport=arrival_airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        elif departure_airport is not None:
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign, day from flights_data4 "
                "where estdepartureairport ='{departure_airport}' "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                departure_airport=departure_airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        elif airport is not None:
            other_tables += (
                "join (select icao24 as e_icao24, firstseen, "
                "estdepartureairport, lastseen, estarrivalairport, "
                "callsign, day from flights_data4 "
                "where (estdepartureairport ='{arrival_or_departure_airport}' "
                "or estarrivalairport = '{arrival_or_departure_airport}') "
                "and ({day_min:.0f} <= day and day <= {day_max:.0f})) as est"
            ).format(
                arrival_or_departure_airport=airport,
                day_min=day_min.timestamp(),
                day_max=day_max.timestamp(),
            )

        fst_columns = [field.strip() for field in columns.split(",")]

        if count_airports_params > 1:
            est_columns = [
                "firstseen",
                "estdepartureairport",
                "lastseen",
                "estarrivalairport",
                "day",
            ]
            columns = (
                ", ".join(f"{table_name}.{field}" for field in fst_columns)
                + ", "
                + ", ".join(f"est.{field}" for field in est_columns)
            )
            parse_columns = ", ".join(
                [
                    *fst_columns,
                    "firstseen",
                    "origin",
                    "lastseen",
                    "destination",
                    "day",
                ]
            )
        if bounds is not None:
            columns = (
                ", ".join(f"{table_name}.{field}" for field in fst_columns)
                + ", "
                + ", ".join(
                    f"est.{field}"
                    for field in ["firstseen", "lastseen", "e_icao24"]
                )
            )
            parse_columns = ", ".join(
                [*fst_columns, "firstseen", "lastseen", "icao24_2"]
            )

        sequence = list(split_times(start_ts, stop_ts, date_delta))
        cumul = []

        if limit is not None:
            other_params += f"limit {limit}"

        for bt, at, bh, ah in progressbar(sequence):
            _log.info(
                f"Sending request between time {bt} and {at} "
                f"and hour {bh} and {ah}"
            )

            if "{before_hour}" in other_tables:
                _other_tables = other_tables.format(
                    before_hour=bh.timestamp(), after_hour=ah.timestamp()
                )
            else:
                _other_tables = other_tables

            request = _request.format(
                columns=columns,
                table_name=table_name,
                before_time=int(bt.timestamp()),
                after_time=int(at.timestamp()),
                before_hour=bh.timestamp(),
                after_hour=ah.timestamp(),
                other_tables=_other_tables,
                other_params=other_params,
                where_clause=where_clause,
            )

            df = self._impala(
                request, columns=parse_columns, cached=cached, compress=compress
            )

            if df is None:
                continue

            df = self._format_dataframe(df)

            cumul.append(df)

        if len(cumul) == 0:
            return None

        return pd.concat(cumul)

    def extended(self, *args: Any, **kwargs: Any) -> None | pd.DataFrame:
        return self.rawdata(
            *args, **kwargs, table_name="rollcall_replies_data4"
        )


Impala.extended.__doc__ = Impala.rawdata.__doc__
Impala.extended.__annotations__ = {
    key: value
    for (key, value) in Impala.rawdata.__annotations__.items()
    if key != "table_name"
}

# below this line is only helpful references
# ------------------------------------------
# [hadoop-1:21000] > describe rollcall_replies_data4;
# +----------------------+-------------------+---------+
# | name                 | type              | comment |
# +----------------------+-------------------+---------+
# | sensors              | array<struct<     |         |
# |                      |   serial:int,     |         |
# |                      |   mintime:double, |         |
# |                      |   maxtime:double  |         |
# |                      | >>                |         |
# | rawmsg               | string            |         |
# | mintime              | double            |         |
# | maxtime              | double            |         |
# | msgcount             | bigint            |         |
# | icao24               | string            |         |
# | message              | string            |         |
# | isid                 | boolean           |         |
# | flightstatus         | tinyint           |         |
# | downlinkrequest      | tinyint           |         |
# | utilitymsg           | tinyint           |         |
# | interrogatorid       | tinyint           |         |
# | identifierdesignator | tinyint           |         |
# | valuecode            | smallint          |         |
# | altitude             | double            |         |
# | identity             | string            |         |
# | hour                 | int               |         |
# +----------------------+-------------------+---------+
