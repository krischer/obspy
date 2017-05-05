#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FDSN Web service client for ObsPy.

:class:`~obspy.clients.fdsn.routers.FedcatalogProviders` contains data center
(provider) details retrieved from the fedcatalog service

:class:`~obspy.clients.fdsn.routers.FederatedClient` is the FDSN Web service
request client. The end user will work almost exclusively with this class,
which has methods similar to :class:`~obspy.clients.fdsn.Client`

:class:`~obspy.clients.fdsn.routers.FederatedRoutingManager` provides parsing
capabilities, and helps the FederatedClient make requests to each individual
provider's service

:func:`distribute_args()` helps determine what parameters belong to the routing
service and which belong to the data provider's client service

:func:`get_bulk_string()` helps turn text and parameters into a valid bulk
request text block.

:func:`data_to_request()` helper function to convert
:class:`~obspy.core.inventory.inventory.Inventory` or
:class:`~obpsy.core.Stream` into FDSNBulkRequests. Useful for comparing what
has been retrieved with what was requested.

:copyright:
    The ObsPy Development Team (devs@obspy.org)
    Celso G Reyes, 2017
    IRIS-DMC
:license:
    GNU Lesser General Public License, Version 3
    (https://www.gnu.org/copyleft/lesser.html)
"""
from __future__ import print_function, unicode_literals
import sys
import collections
from threading import Lock
import os
from future.utils import string_types
import requests
from obspy.core.inventory import Inventory
from obspy.core import Stream
from obspy.clients.fdsn.client import convert_to_string
from obspy.clients.fdsn.header import (FDSNException, FDSNNoDataException)
from obspy.clients.fdsn.routers.routing_client import (
    RoutingClient, RoutingManager, ROUTING_LOGGER)
from obspy.clients.fdsn.routers import (FederatedRoute,)
from obspy.clients.fdsn.routers.fedcatalog_parser import (
    PreParse, FedcatResponseLine, DatacenterItem, inventory_to_bulkrequests,
    stream_to_bulkrequests)


# IRIS uses different codes for datacenters than obspy.
#         (iris_name , obspy_name)
REMAPS = (("IRISDMC", "IRIS"),
          ("GEOFON", "GFZ"),
          ("SED", "ETH"),
          ("USPC", "USP"))

FEDCATALOG_URL = 'https://service.iris.edu/irisws/fedcatalog/1/'


def distribute_args(argdict):
    """
    divide a dictionary's keys between fedcatalog and provider's service

    When the FederatedClient is called with a bunch of keyword arguments,
    it should call the Fedcatalog service with a large subset of these.
    Most will be incorporated into the bulk data requests that will be
    sent to the client's service. However a few of these are not allowed
    to be passed in this way.  These are prohibited, and will be removed
    from the fedcat_kwargs.

    The client's service will not need most of these keywords, since
    they are included in the bulk request.  However, some keywords are
    required by the Client class, so they are allowed through.

    :type argdict: dict
    :param argdict: keyword arugments that were passed to the FederatedClient
    :rtype: tuple(dict() , dict())
    :returns: tuple of dictionaries fedcat_kwargs, fdsn_kwargs
    """

    fedcatalog_prohibited = ('filename', 'attach_response', 'user',
                             'password', 'base_url')
    service_allowed = ('user', 'password', 'attach_response', 'filename')

    # fedrequest gets almost all arguments, except for some
    fed_argdict = argdict.copy()
    for key in fedcatalog_prohibited:
        if key in fed_argdict:
            del fed_argdict[key]

    # services get practically no arguments: args provided by the bulk request
    service_args = dict()
    for key in service_allowed:
        if key in argdict:
            service_args[key] = argdict[key]
    return fed_argdict, service_args


def get_bulk_string(bulk, arguments):
    """
    simplified version of get_bulk_string used for bulk requests

    This was mostly pulled from the :class:`~obspy.clients.fdsn.Client`,
    because it does not need to be associated with the client class.

    :type bulk: string, file
    :param bulk:
    :type arguments: dict
    :param arguments: key-value pairs to be added to the bulk request
    :rtype: str
    :returns: bulk request str suitable for sending to a client's get service
    """
    # If its an iterable, we build up the query string from it
    # StringIO objects also have __iter__ so check for 'read' as well

    if arguments is not None:
        args = ["%s=%s" % (key, convert_to_string(value))
                for key, value in arguments.items() if value is not None]
    else:
        args = None

    # bulk might be tuple of strings...
    if isinstance(bulk, string_types):
        tmp = bulk
    elif isinstance(bulk, collections.Iterable) and not hasattr(bulk, "read"):
        msg = "fedcatalog's get_bulk_string cannot handle vectors."
        raise NotImplementedError(msg)
    else:
        # if it has a read method, read data from there
        if hasattr(bulk, "read"):
            tmp = bulk.read()
        elif isinstance(bulk, string_types):
            # check if bulk is a local file
            if "\n" not in bulk and os.path.isfile(bulk):
                with open(bulk, 'r') as fileh:
                    tmp = fileh.read()
            # just use bulk as input data
            else:
                tmp = bulk
        else:
            msg = ("Unrecognized input for 'bulk' argument. Please "
                   "contact developers if you think this is a bug.")
            raise NotImplementedError(msg)
    if args:
        args = '\n'.join(args)
        bulk = '\n'.join((args, tmp))
    else:
        bulk = tmp
    assert isinstance(bulk, string_types)
    return bulk


def get_existing_route(existing_routes):
    # does not know how to read from file.
    # load routes into a FederatedRoutingManager first
    if isinstance(existing_routes, FederatedRoutingManager):
        frm = existing_routes
    elif isinstance(existing_routes, (string_types, FederatedRoute)):
        frm = FederatedRoutingManager(existing_routes)
    else:
        msg = "unsure how to convert %s into FederatedRoutingManager"
        NotImplementedError(msg % existing_routes.__class__)
    return frm


class FedcatalogProviders(object):
    """
    Class containing datacenter details retrieved from the fedcatalog service

    keys: name, website, lastupdate, serviceURLs {servicename:url,...},
    location, description

    >>> prov = FedcatalogProviders()
    >>> print(prov.pretty('IRISDMC'))  #doctest: +ELLIPSIS
    IRISDMC:The IRIS Data Management Center...M

    """

    def __init__(self):
        """
        Initializer for FedcatalogProviders
        """
        self._providers = dict()
        self._lock = Lock()
        self._failed_refreshes = 0
        self.refresh()

    def __iter__(self):
        """
        iterate through each provider name

        >>> fcp=FedcatalogProviders()
        >>> for k in fcp:
        ...    print k  #  doctest: +SKIP
        """
        return self._providers.__iter__()

    def __getitem__(self, key):
        return self._providers[key]

    @property
    def names(self):
        """
        get names of datacenters

        :rtype: list of str
        :returns: identifiers for fedcatalog providers

        >>> fcp=FedcatalogProviders()
        >>> print(', '.join(sorted(fcp.names)))  #doctest: +ELLIPSIS
        BGR,..., USPSC
        """
        return self._providers.keys()

    def get(self, name, detail=None):
        """
        get a datacenter property

        :type name: str
        :param name: provider name. such as IRISDMC, ORFEUS, etc.
        :type detail: str
        :param detail: property of interest.  eg, one of ('name', 'website',
        'lastupdate', 'serviceURLs', 'location', 'description').
        :rtype: str or dict()
        :returns: detail, or entire dict for the requested provider
        will be returned

        >>> fcp = FedcatalogProviders()
        >>> print(fcp.get('ORFEUS','description'))
        The ORFEUS Data Center
        """
        if name not in self._providers:
            return ""
        else:
            if detail:
                return self._providers[name][detail]
            else:
                return str(self._providers[name])

    def refresh(self, force=False):
        """
        retrieve provider profile from fedcatalog service

        >>> providers = FedcatalogProviders()
        >>> # providers.refresh(force=True)
        >>> n =sorted(providers.names)
        >>> print(','.join(n)) #doctest: +ELLIPSIS
        BGR,...,USPSC

        :type force: bool
        :param force: attempt to retrieve data even if it already exists
        or if too many attempts have failed
        """
        if self._providers and not force:
            return
        if self._lock.locked():
            return
        with self._lock:
            # ROUTING_LOGGER.debug("Refreshing Provider List")
            if self._failed_refreshes > 3 and not force:
                msg = "Unable to retrieve provider profiles from"\
                      " fedcatalog after %d attempts"
                ROUTING_LOGGER.error(msg, self._failed_refreshes)
            url = 'https://service.iris.edu/irisws/fedcatalog/1/datacenters'
            try:
                resp = requests.get(url, verify=False)
                self._providers = {v['name']: v for v in resp.json()}
                self._failed_refreshes = 0
            except Exception as err:
                msg = "Unable to update provider profiles: %s"
                ROUTING_LOGGER.error(msg, err)
                self._failed_refreshes += 1
                return

            for iris_name, obspy_name in REMAPS:
                if iris_name in self._providers:
                    self._providers[obspy_name] = self._providers[iris_name]

    def pretty(self, name):
        """
        return nice text representation of service without too much details

        >>> providers = FedcatalogProviders()
        >>> print(providers.pretty("ORFEUS"))  #doctest: +ELLIPSIS
        ORFEUS:The ORFEUS Data Center, de Bilt, the Netherlands ...M
        >>> print(providers.pretty("IRIS") == providers.pretty("IRISDMC"))
        True

        :type name: str
        :param name: identifier provider (provider_id)
        :rtype: str
        :returns: formatted details about this provider
        """
        if name not in self._providers:
            return ""
        return "{name}:{description}, {location} WEB:{website}"\
               "LastUpdate:{lastUpdate}".format(**self._providers[name])


PROVIDERS = FedcatalogProviders()


class FederatedClient(RoutingClient):
    """
    FDSN Web service request client.

    For details see the :meth:`~obspy.clients.fdsn.client.Client.__init__()`
    method.

    >>> from requests.packages.urllib3.exceptions import InsecureRequestWarning
    >>> requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    >>> client = FederatedClient()
    >>> print(client)  #doctest: +ELLIPSIS
    Federated Catalog Routing Client

    >>> inv = client.get_stations(network="I?", station="AN*", channel="*HZ")
    ...                           #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
    >>> print(inv)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
    Inventory created at ...Z
        Created by: IRIS WEB SERVICE: fdsnws-station | version: 1...
                http://service.iris.edu/fdsnws/station/1/query
        Sending institution: IRIS-DMC (IRIS-DMC)
        Contains:
            Networks (1):
                IU
            Stations (10):
                IU.ANMO (Albuquerque, New Mexico, USA) (6x)
                IU.ANTO (Ankara, Turkey) (4x)
            Channels (0):
    <BLANKLINE>

    >>> inv = client.get_stations(network="I?", station="AN*", channel="*HZ",
    ...                           filename=sys.stderr) #doctest: +SKIP

    .. Warning: if output is sent directly to a file, then the success
                status will not be checked beyond gross failures, such as
                no data, no response, or a timeout
    """

    def __init__(self, **kwargs):
        """
        initializer for FederatedClient

        :type **kwargs: keyword arguments
        :param **kwargs: arguments destined for either Fedcatalog or Client
        """
        RoutingClient.__init__(self, **kwargs)
        PROVIDERS.refresh()

    def __str__(self):
        """
        String representation for FederatedClient

        :rtype: str
        :returns: string represention of the FederatedClient
        """
        part1 = "Federated Catalog Routing Client\n"
        part2 = "  request-method: %s\n" % \
                ("parallel" if self.use_parallel else "serial")
        if self.include_provider:
            return part1 + part2 + "  include: %s" % \
                (",".join(self.include_provider))
        if self.exclude_provider:
            return part1 + part2 + "  exclude: %s" %\
                (",".join(self.exclude_provider))
        return part1 + part2

    # -------------------------------------------------
    # FederatedClient.get_routing() and FederatedClient.get_routing_bulk()
    # communicate directly with the fedcatalog service
    # -------------------------------------------------
    def get_routing(self, routing_file=None, **kwargs):
        """
        send query to the fedcatalog service as param=value pairs (GET)

        Retrieves and parses routing details from the fedcatalog service,
        which takes a query, determines which datacenters/providers hold
        the appropriate data, and then returns information about the holdings

        :type routing_file: str
        :param routing_file: filename used to write out raw fedcatalog response
        :type **kwargs: various
        :param **kwargs: arguments to be passed to the fedcatalog service
        as GET parameters.  eg ... http://.../query?param1=val1&param2=val2&...
        :rtype: :class:`~obspy.clients.fdsn.routers.FederatedRoutingManager`
        :returns: parsed response from the FedCatalog service

        >>> client = FederatedClient()
        >>> params = {"station":"ANTO", "includeoverlaps":"true"}
        >>> frm = client.get_routing(**params)
        >>> for f in frm:
        ...   print(f)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        FederatedRoute for IRIS containing 0 query parameters ... request items
        FederatedRoute for ORFEUS containing 0 ... and ... request items

        """
        if 'bulk' in kwargs:
            ValueError("To post a bulk request, use get_routing_bulk")
        query_url = FEDCATALOG_URL + "query"
        resp = requests.get(query_url, params=kwargs, verify=False)
        resp.raise_for_status()

        if routing_file is not None:
            if isinstance(routing_file, string_types):
                with open(routing_file, 'wb') as fileh:
                    fileh.write(resp.text)
            else:
                # assume it is an open file-like-type
                routing_file.write(resp.text)

        frm = FederatedRoutingManager(resp.text)
        return frm

    def get_routing_bulk(self, bulk, routing_file=None, **kwargs):
        """
        send query to the fedcatalog service as a POST.

        Retrieves and parses routing details from the fedcatalog service,
        which takes a bulk request, determines which datacenters/providers hold
        the appropriate data, and then sends back holdings information
        :type bulk: str, iterable of str
        :param bulk:
        :type routing_file:
        :param routing_file: file to write out raw fedcatalog response
        :type **kwargs: other parameters
        :param **kwargs: only kwargs that should go to fedcatalog
        :rtype: :class:`~obspy.clients.fdsn.routers.FederatedRoutingManager`
        :returns: parsed response from the FedCatalog service

        >>> client = FederatedClient()
        >>> params={"includeoverlaps":"true"}
        >>> frm = client.get_routing_bulk(bulk="* ANTO * * * *", **params)
        >>> for f in frm:
        ...   print(f)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        FederatedRoute for IRIS ... 0 query parameters and ... request items
        FederatedRoute for ORFEUS containing 0 ... and ... request items

        """
        if not isinstance(bulk, string_types)\
                and isinstance(bulk, collections.Iterable):
            print(bulk, file=sys.stderr)
            bulk = get_bulk_string(bulk=bulk, arguments=kwargs)
        elif isinstance(bulk, string_types) and kwargs:
            bulk = get_bulk_string(bulk=bulk, arguments=kwargs)

        assert bulk, "Bulk is empty after homogenizing it via get_bulk_string"
        assert isinstance(bulk, string_types), \
            "Bulk should be a string, but is a " + bulk.__class__.__name__

        resp = requests.post(FEDCATALOG_URL + "query", data=bulk, verify=False)
        resp.raise_for_status()
        if routing_file is not None:
            if isinstance(routing_file, string_types):
                with open(routing_file, 'wb') as fileh:
                    fileh.write(resp.text)
            else:
                # assume it is an open file-like-type
                routing_file.write(resp.text)

        frm = FederatedRoutingManager(resp.text)
        return frm

    # -------------------------------------------------
    # The next routines will interface with the "regular" client
    # FederatedClient._request() : overloads the RoutingClient,
    #                              called by the query method
    # FederatedClient.get_stations_bulk(): user facing method
    # FederatedClient.get_stations(): user facing method
    # FederatedClient.get_waveforms_bulk(): user facing method
    # FederatedClient.get_waveforms(): user facing method
    #
    # communicate directly with the obspy.fdsn.Client's service:
    #     eg. dataselect, station
    # -------------------------------------------------

    def _request(self, client=None, service=None, route=None, output=None,
                 passed=None, failed=None, filename=None, **kwargs):
        """
        function used to query FDSN webservice

        This is being called from one of the "...query_machine" methods
        of the RoutingClient.

        :meth:`~obspy.clients.fdsn.client.Client.get_waveforms_bulk` or
        :meth:`~obspy.clients.fdsn.client.Client.get_stations_bulk`

        :type client: :class:`~obspy.clients.fdsn.Client`
        :param client: client, associated with a datacenter
        :type service: str
        :param service: name of service, "DATASELECTSERVICE", "STATIONSERVICE"
        :type route: :class:`~obspy.clients.fdsn.route.FederatedRoute`
        :param route: used to provide
        :type output: queue
        :param output: place where retrieved data go (unless filename used)
        :type failed: queue
        :param failed: place where list of unretrieved bulk request lines go
        :type filename: str or open file handle
        :param filename: filename for streaming data from service
        :type **kwargs: various
        :param **kwargs: keyword arguments passed directly to the client's
        get_waveform_bulk() or get_stations_bulk() method.
        """

        bulk_services = {"DATASELECTSERVICE": client.get_waveforms_bulk,
                         "STATIONSERVICE": client.get_stations_bulk}

        # communicate via queues or similar. Therefore, make containers exist,
        # and have the 'put' routine
        assert service in bulk_services, "couldn't find {0}\n".format(service)
        assert route is not None, "missing route"
        assert filename or output is not None, \
            "missing container for storing output [output]"
        assert filename or hasattr(output, 'put'), \
            "'output' does not have a 'put' routine"
        assert passed is not None, \
            "missing container for storing successful requests [passed]"
        assert hasattr(passed, 'put'), \
            "'passed' does not have a 'put' routine"
        assert failed is not None, \
            "missing container for storing failed requests [failed]"
        assert hasattr(failed, 'put'), \
            "'failed' does not have a 'put' routine"

        try:
            # get_bulk is the client's "get_xxx_bulk" function.
            get_bulk = bulk_services.get(service)
        except ValueError:
            valid_services = '"' + ', '.join(bulk_services.keys)
            raise ValueError(
                "Expected one of %s bug got %s" % (valid_services, service))

        try:
            if isinstance(filename, string_types):
                base_name = os.path.basename(filename)
                path_name = os.path.dirname(filename)
                base_name = '-'.join((route.provider_id, base_name))
                filename = os.path.join(path_name, base_name)
                ROUTING_LOGGER.info("sending file to :" + filename)
            if filename:
                get_bulk(route.text(service), filename=filename, **kwargs)
                # nothing gets put into the output or passed queues
                return

            data = get_bulk(route.text(service), filename=filename, **kwargs)
            req_details = data_to_request(data)
            ROUTING_LOGGER.info("Retrieved %d items from %s",
                                len(req_details), route.provider_id)
            # ROUTING_LOGGER.info('\n' + str(req_details))
            output.put(data)
            passed.put(req_details)

        except FDSNNoDataException:
            failed.put(route.request_items)
            ROUTING_LOGGER.info("The provider %s could provide no data",
                                route.provider_id)

        except FDSNException as ex:
            failed.put(route.request_items)
            print("Failed to retrieve data from: {0}", route.provider_id)
            print(ex)
            raise

    def get_waveforms_bulk(self, bulk, quality=None, minimumlength=None,
                           longestonly=None, filename=None,
                           includeoverlaps=False, reroute=False,
                           existing_routes=None, **kwargs):
        """
        retrieve waveforms from providers via POST to the Fedcatalog service

        :type includeoverlaps: boolean
        :param includeoverlaps: retrieve same information from multiple sources
        (not recommended)
        :type reroute: boolean
        :param reroute: if data doesn't arrive from provider , check elsewhere
        :type existing_routes: str
        :param existing_routes: will skip initial query to fedcatalog service,
        instead using the information from here to make the queries.
        :rtype: :class:`~obspy.core.stream.Stream`
        :returns: one or more traces in a stream

        other parameters can be used, as seen in:
             :meth:`~obspy.fdsn.clients.Client.get_waveforms_bulk`
             :meth:`~obspy.fdsn.clients.Client.get_stations_bulk`

        >>> client = FederatedClient()
        >>> bulkreq = "IU ANMO * ?HZ 2010-02-27T06:30:00 2010-02-27T06:33:00"
        >>> tr = client.get_waveforms_bulk(bulk=bulkreq)
        ...        #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        >>> print(tr)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        6 Trace(s) in Stream:
        IU.ANMO.00.BHZ | 2010-02-27T06:30... | 20.0 Hz, 3600 samples
        IU.ANMO.00.LHZ | 2010-02-27T06:30... | 1.0 Hz, 180 samples
        ...
        IU.ANMO.10.VHZ | 2010-02-27T06:30... | 0.1 Hz, 18 samples

        """

        svc_name = 'DATASELECTSERVICE'
        fed_kwargs, svc_kwargs = distribute_args(kwargs)
        fed_kwargs["includeoverlaps"] = includeoverlaps

        frm = self.get_routing_bulk(
            bulk=bulk, **fed_kwargs)\
            if not existing_routes else get_existing_route(existing_routes)
        data, _, failed = self.query(frm, svc_name, **svc_kwargs)

        if reroute and failed:
            data = self.attempt_reroute(failed, svc_name, fed_kwargs,
                                        svc_kwargs, data)

        if not data:
            raise FDSNNoDataException("No data available for request.")
        return data

    def get_waveforms(self, network, station, location, channel, starttime,
                      endtime, includeoverlaps=False, reroute=False,
                      existing_routes=None, **kwargs):
        """
        retrieve waveforms from providers via GET request to Fedcatalog service

        :type includeoverlaps: boolean
        :param includeoverlaps: retrieve same information from multiple sources
        (not recommended)
        :type reroute: boolean
        :param reroute: if data doesn't arrive from provider, look elsewhere
        :type existing_routes: str, FederatedRoutingManager, FederatedRoute
        :param existing_routes: will skip initial query to fedcatalog service,
        instead use existing routes in a FederatedRoutingManager
        :rtype: :class:`~obspy.core.stream.Stream`
        :returns: one or more traces in a stream
        other parameters as seen in
            :meth:`~obspy.fdsn.clients.Client.get_waveforms`

        >>> from requests.packages.urllib3.exceptions import InsecureRequestWarning
        >>> requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        >>> client = FederatedClient()
        >>> from obspy.core import  UTCDateTime
        >>> t_st = UTCDateTime("2010-02-27T06:30:00")
        >>> t_ed = UTCDateTime("2010-02-27T06:33:00")
        >>> tr = client.get_waveforms('IU', 'ANMO', '*', 'BHZ', t_st, t_ed)
        >>> print(tr)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        2 Trace(s) in Stream:
        IU.ANMO.00.BHZ | 2010-02-27T06:30:00... 20.0 Hz, 3600 samples
        IU.ANMO.10.BHZ | 2010-02-27T06:30:00... 40.0 Hz, 7200 samples
        """

        svc_name = 'DATASELECTSERVICE'
        fed_kwargs, svc_kwargs = distribute_args(kwargs)
        fed_kwargs["includeoverlaps"] = includeoverlaps
        assert "bulk" not in fed_kwargs, \
               "Bulk request should be sent to get_waveforms_bulk instead"

        frm = self.get_routing(network=network, station=station,
                               location=location, channel=channel,
                               starttime=starttime, endtime=endtime,
                               **fed_kwargs) if not existing_routes \
            else get_existing_route(existing_routes)

        data, _, failed = self.query(frm, svc_name, **svc_kwargs)

        if reroute and failed:
            data = self.attempt_reroute(failed, svc_name, fed_kwargs,
                                        svc_kwargs, data)

        if not data:
            raise FDSNNoDataException("No data available for request.")
        return data

    def get_stations_bulk(self, bulk, includeoverlaps=False, reroute=False,
                          existing_routes=None, **kwargs):
        """
        retrieve station metadata from data providers via POST request
        to the Fedcatalog service

        :type bulk: text (bulk request formatted)
        :param bulk: text containing request to send to router
        :type includeoverlaps: boolean
        :param includeoverlaps: retrieve same information from multiple sources
        (not recommended)
        :type reroute: boolean
        :param reroute: if data doesn't arrive from provider , see if it is
            available elsewhere
        :type existing_routes: str
        :param existing_routes: will skip initial query to fedcatalog service,
            instead.  To use an existing route, set bulk to "none"
        :rtype: :class:`~obspy.core.inventory.inventory.Inventory`
        :returns: an inventory tree containing network/station/channel metadata

        other parameters as seen in
            :meth:`~obspy.fdsn.clients.Client.get_stations_bulk`

        >>> from requests.packages.urllib3.exceptions import InsecureRequestWarning
        >>> requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        >>> client = FederatedClient()
        >>> bulktxt = "level=channel\\nA? OKS? * ?HZ * *"
        >>> INV = client.get_stations_bulk(bulktxt)  #doctest: +ELLIPSIS
        >>> print(INV)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        Inventory created at 2...Z
            Created by: IRIS WEB SERVICE: fdsnws-station | version: 1...
                    http://service.iris.edu/fdsnws/station/1/query
            Sending institution: IRIS-DMC (IRIS-DMC)
            Contains:
                Networks (1):
                    AV
                Stations (2):
                    AV.OKSO (South, Okmok Caldera, Alaska)
                    AV.OKSP (Steeple Point, Okmok Caldera, Alaska)
                Channels (5):
                    AV.OKSO..BHZ, AV.OKSP..EHZ (4x)
        >>> fed_client = FederatedClient(use_parallel=True)
        >>> bulktext = "IU ANTO * BHZ 2015-01-01T00:00:00 2015-02-01T00:00:00"
        >>> inv = fed_client.get_stations_bulk(bulktext, includeoverlaps=True)
        >>> print(inv)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        Inventory created at ...
            Sending institution: IRIS-DMC,SeisComP3 (IRIS-DMC,ODC)
            Contains:
                Networks (2):
                    IU (2x)
                Stations (2):
                    IU.ANTO (Ankara, Turkey) (2x)
                Channels (0):
        <BLANKLINE>
        """

        svc_name = 'STATIONSERVICE'
        fed_kwargs, svc_kwargs = distribute_args(kwargs)
        fed_kwargs["includeoverlaps"] = includeoverlaps

        frm = self.get_routing_bulk(bulk=bulk, **fed_kwargs)\
            if not existing_routes else get_existing_route(existing_routes)

        # frm = self.get_routing_bulk(bulk=bulk, **fed_kwargs)
        inv, _, failed = self.query(frm, svc_name, **svc_kwargs)

        if reroute and failed:
            inv = self.attempt_reroute(failed, svc_name, fed_kwargs,
                                       svc_kwargs, inv)

        if not inv:
            raise FDSNNoDataException("No data available for request.")
        return inv

    def get_stations(self, includeoverlaps=False, reroute=False,
                     existing_routes=None, **kwargs):
        """
        retrieve metadata from providers via GET request to Fedcatalog service

        :type includeoverlaps: boolean
        :param includeoverlaps: retrieve same information from multiple sources
        (not recommended)
        :type reroute: boolean
        :param reroute: if data doesn't arrive from provider , check elsewhere
        :type existing_routes: str
        :param existing_routes: will skip initial query to fedcatalog service,
        instead using the information from here to make the queries.
        :rtype: :class:`~obspy.core.inventory.inventory.Inventory`
        :returns: an inventory tree containing network/station/channel metadata

        other parameters as seen in
            :meth:`~obspy.fdsn.clients.Client.get_stations`

        >>> from requests.packages.urllib3.exceptions import InsecureRequestWarning
        >>> requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        >>> fclient = FederatedClient()
        >>> INV = fclient.get_stations(network="A?", station="OK*",
        ...                           channel="?HZ", level="station",
        ...                           endtime="2016-12-31")
        >>> print(INV)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        Inventory created at 2...Z
            Created by: IRIS WEB SERVICE: fdsnws-station | version: 1...
                    http://service.iris.edu/fdsnws/station/1/query
            Sending institution: IRIS-DMC (IRIS-DMC)
            Contains:
                Networks (1):
                    AV
                Stations (14):
                    AV.OKAK (Cape Aslik 2, Okmok Caldera, Alaska)
                    ...
                    AV.OKWR (West Rim, Okmok Caldera, Alaska)
                Channels (0):
        <BLANKLINE>

        Exclude a provider from being queried

        >>> keep_out = ["IRISDMC","IRIS","IRIS-DMC"]
        >>> fclient.exclude_provider = keep_out
        >>> INV2 = fclient.get_stations(network="I?", station="A*",
        ...                           starttime="2013-01-01", level="network")
        >>> print(INV2)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        Inventory created at ...Z
            Created by: ObsPy ...
                    https://www.obspy.org
            Sending institution: SeisComP3,SeisNet-mysql (GFZ,INGV-CNT,ODC)
            Contains:
                Networks (5):
                    IA, II, IQ, IS, IV
                Stations (0):...
                Channels (0):...

        >>> fclient = FederatedClient(use_parallel=True)

        parallel request, but only one provider

        >>> INV = fclient.get_stations(network="A?", station="OK*",
        ...                           channel="?HZ", level="station",
        ...                           endtime="2016-12-31")
        >>> print(INV)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        Inventory created at 2...Z
            Created by: IRIS WEB SERVICE: fdsnws-station | version: 1...
                    http://service.iris.edu/fdsnws/station/1/query
            Sending institution: IRIS-DMC (IRIS-DMC)
            Contains:
                Networks (1):
                    AV
                Stations (14):
                    AV.OKAK (Cape Aslik 2, Okmok Caldera, Alaska)
                    ...
                    AV.OKWR (West Rim, Okmok Caldera, Alaska)
                Channels (0):
        <BLANKLINE>

        another parallel request, this time with several providers

        >>> INV2 = fclient.get_stations(network="I?", station="AN*",
        ...                             channel="?HZ", level="network",
        ...                             includeoverlaps="true")
        >>> print(INV2)  #doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        Inventory created at ...Z
            Created by: ObsPy ...
                    https://www.obspy.org
            Sending institution: IRIS-DMC,SeisComP3 (IRIS-DMC,ODC)
            Contains:
                Networks (...):
                    IU (...)
                Stations (0):...
                Channels (0):...
        """

        svc_name = "STATIONSERVICE"
        fed_kwargs, svc_kwargs = distribute_args(kwargs)
        fed_kwargs["includeoverlaps"] = includeoverlaps

        assert "bulk" not in fed_kwargs, \
               "use get_stations_bulk for bulk requests"

        frm = self.get_routing(**fed_kwargs) if not existing_routes \
            else get_existing_route(existing_routes)

        # query queries all providers
        inv, _, failed = self.query(frm, svc_name, **svc_kwargs)

        if reroute and failed:
            inv = self.attempt_reroute(failed, svc_name, fed_kwargs,
                                       svc_kwargs, inv)

        if not inv:
            raise FDSNNoDataException("No data available for request.")
        return inv

    def attempt_reroute(self, bulk, svc_name, fed_kwargs, svc_kwargs,
                        existing_data):
        """
        request missing data from any provider that might have it

        :type bulk: FDSNBulkRequests
        :param bulk: items that require re-requesting
        :type svc_name: str
        :param svc_name: 'STATIONSERVICE' or 'DATASELECTSERVICE', as per
        service name included in the Fedcatalog response
        :type fed_kwargs: dict
        :param fed_kwargs: keyword argumentspassed to the Fedcatalog service
        :type svc_kwargs: dict
        :param svc_kwargs: keyword arguments to be passed to service endpoint
        :type existing_data: :class:`~obspy.core.inventory.inventory.Inventory`
        or :class:`~obspy.core.Stream`
        :param existing_data: successfully retrieved data
        :rtype: same as existing_data
        :return: additional data (if any) added to existing_data

        This cannot be used in conjunction with filename
        The simple appending/extending of data will not rearrange existing data
        That is, inventory trees will not be merged.
        """
        assert "filename" not in svc_kwargs,\
            "Rerouting doesn't work for items sent to file"
        # what about if svc_kwargs has filename (?)
        ROUTING_LOGGER.info(
            "%d items were not retrieved, trying again, but from any provider"
            " (while honoring include/exclude)", len(bulk))
        fed_kwargs["includeoverlaps"] = True
        frm = self.get_routing_bulk(bulk=str(bulk), **fed_kwargs)
        more_data, passed, failed = self.query(frm, svc_name, keep_unique=True,
                                               **svc_kwargs)

        if more_data:
            ROUTING_LOGGER.info("Retrieved %d additional items:\n%s",
                                len(passed), passed)
            if existing_data:
                existing_data += more_data
            else:
                existing_data = more_data
        if failed:
            ROUTING_LOGGER.info("Unable to retrieve %d items:\n%s",
                                len(failed), failed)
        return existing_data


class FederatedRoutingManager(RoutingManager):
    """
    This class wraps the response given by the federated catalog.  Its primary
    purpose is to divide the response into parcels, each being a
    FederatedRoute containing the information required for a single request.

    Input would be the response from the federated catalog, or a similar text
    file. Output is a list of FederatedRoute objects

    >>> from obspy.clients.fdsn import Client
    >>> url = 'https://service.iris.edu/irisws/fedcatalog/1/'
    >>> params = {"net":"A*", "sta":"OK*", "cha":"*HZ"}
    >>> r = requests.get(url + "query", params=params, verify=False)
    >>> frm = FederatedRoutingManager(r.text)
    >>> print(frm)
    FederatedRoutingManager with 1 items:
    FederatedRoute for IRIS containing 0 query parameters and 26 request items
    """

    def __init__(self, data):
        """
        initialize a FederatedRoutingManager object
        :type data: str
        :param data: text block
        :
        """
        RoutingManager.__init__(self, data, provider_details=PROVIDERS)

    def parse_routing(self, block_text):
        """
        create a list of FederatedRoute objects, one for each provider

        :type block_text:
        :param block_text:
        :rtype:
        :returns:

        >>> fed_text = '''minlat=34.0
        ... level=network
        ...
        ... DATACENTER=GEOFON,http://geofon.gfz-potsdam.de
        ... DATASELECTSERVICE=http://geofon.gfz-potsdam.de/fdsnws/dataselect/1/
        ... CK ASHT -- HHZ 2015-01-01T00:00:00 2016-01-02T00:00:00
        ...
        ... DATACENTER=INGV,http://www.ingv.it
        ... STATIONSERVICE=http://webservices.rm.ingv.it/fdsnws/station/1/
        ... HL ARG -- BHZ 2015-01-01T00:00:00 2016-01-02T00:00:00
        ... HL ARG -- VHZ 2015-01-01T00:00:00 2016-01-02T00:00:00'''
        >>> fr = FederatedRoutingManager(fed_text)
        >>> for f in fr:
        ...    print(f.provider_id + "\\n" + f.text('STATIONSERVICE'))
        GFZ
        level=network
        CK ASHT -- HHZ 2015-01-01T00:00:00.000 2016-01-02T00:00:00.000
        INGV
        level=network
        HL ARG -- BHZ 2015-01-01T00:00:00.000 2016-01-02T00:00:00.000
        HL ARG -- VHZ 2015-01-01T00:00:00.000 2016-01-02T00:00:00.000

        Here's an example parsing from the actual service:
        >>> import requests
        >>> from requests.packages.urllib3.exceptions import InsecureRequestWarning
        >>> requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        >>> url = 'https://service.iris.edu/irisws/fedcatalog/1/query'
        >>> r = requests.get(url, params={"net":"IU", "sta":"ANTO",
        ...                  "cha":"BHZ", "endafter":"2013-01-01",
        ...                  "includeoverlaps":"true", "level":"station"},
        ...                  verify=False)
        >>> frp = FederatedRoutingManager(r.text)
        >>> for n in frp:
        ...     print(n.services["STATIONSERVICE"])
        ...     print(n.text("STATIONSERVICE"))
        http://service.iris.edu/fdsnws/station/1/
        level=station
        IU ANTO 00 BHZ 2010-11-10T21:42:00.000 2016-06-22T00:00:00.000
        IU ANTO 00 BHZ 2016-06-22T00:00:00.000 2599-12-31T23:59:59.000
        IU ANTO 10 BHZ 2010-11-11T09:23:59.000 2599-12-31T23:59:59.000
        http://www.orfeus-eu.org/fdsnws/station/1/
        level=station
        IU ANTO 00 BHZ 2010-11-10T21:42:00.000 2599-12-31T23:59:59.000
        IU ANTO 10 BHZ 2010-11-11T09:23:59.000 2599-12-31T23:59:59.000

        """

        fed_resp = []
        provider = FederatedRoute("EMPTY_EMPTY_EMPTY")
        parameters = None
        state = PreParse

        for raw_line in block_text.splitlines():
            line = FedcatResponseLine(raw_line)  # use a smarter, trimmed line
            state = state.next(line)
            if state == DatacenterItem:
                if provider.provider_id == "EMPTY_EMPTY_EMPTY":
                    parameters = provider.parameters
                provider = state.parse(line, provider)
                provider.parameters = parameters
                fed_resp.append(provider)
            else:
                state.parse(line, provider)
        if len(fed_resp) > 0 and (not fed_resp[-1].request_items):
            del fed_resp[-1]

        remap = {
            "IRISDMC": "IRIS",
            "GEOFON": "GFZ",
            "SED": "ETH",
            "USPSC": "USP"
        }

        # remap provider codes because IRIS codes differ from OBSPY codes
        for prov in fed_resp:
            if prov.provider_id in remap:
                prov.provider_id = remap[prov.provider_id]
        return fed_resp


def data_to_request(data):
    """
    convert either station metadata or waveform data to FDSNBulkRequests

    :rtype: :class:`~obspy.clients.fdsn.routers.FDSNBulkRequests`
    :returns: representation of the data
    """
    if isinstance(data, Inventory):
        return inventory_to_bulkrequests(data)
    elif isinstance(data, Stream):
        return stream_to_bulkrequests(data)


if __name__ == '__main__':
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    import doctest

    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    doctest.testmod(exclude_empty=True)
