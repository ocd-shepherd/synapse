# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from synapse.crypto.keyclient import fetch_server_key
from twisted.internet import defer
from syutil.crypto.jsonsign import (
    verify_signed_json, signature_ids, sign_json, encode_canonical_json
)
from syutil.crypto.signing_key import (
    is_signing_algorithm_supported, decode_verify_key_bytes
)
from syutil.base64util import decode_base64, encode_base64
from synapse.api.errors import SynapseError, Codes

from synapse.util.retryutils import get_retry_limiter
from synapse.util import unwrapFirstError

from synapse.util.async import ObservableDeferred

from OpenSSL import crypto

from collections import namedtuple
import urllib
import hashlib
import logging


logger = logging.getLogger(__name__)


KeyGroup = namedtuple("KeyGroup", ("server_name", "group_id", "key_ids"))


class Keyring(object):
    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self.client = hs.get_http_client()
        self.config = hs.get_config()
        self.perspective_servers = self.config.perspectives
        self.hs = hs

        self.key_downloads = {}

    def verify_json_for_server(self, server_name, json_object):
        return self.verify_json_objects_for_server(
            [(server_name, json_object)]
        )[0]

    def verify_json_objects_for_server(self, server_and_json):
        """Bulk verfies signatures of json objects, bulk fetching keys as
        necessary.

        Args:
            server_and_json (list): List of pairs of (server_name, json_object)

        Returns:
            list of deferreds indicating success or failure to verify each
            json object's signature for the given server_name.
        """
        group_id_to_json = {}
        group_id_to_group = {}
        group_ids = []

        next_group_id = 0
        deferreds = {}

        for server_name, json_object in server_and_json:
            logger.debug("Verifying for %s", server_name)
            group_id = next_group_id
            next_group_id += 1
            group_ids.append(group_id)

            key_ids = signature_ids(json_object, server_name)
            if not key_ids:
                deferreds[group_id] = defer.fail(SynapseError(
                    400,
                    "Not signed with a supported algorithm",
                    Codes.UNAUTHORIZED,
                ))
            else:
                deferreds[group_id] = defer.Deferred()

            group = KeyGroup(server_name, group_id, key_ids)

            group_id_to_group[group_id] = group
            group_id_to_json[group_id] = json_object

        @defer.inlineCallbacks
        def handle_key_deferred(group, deferred):
            server_name = group.server_name
            try:
                _, _, key_id, verify_key = yield deferred
            except IOError as e:
                logger.warn(
                    "Got IOError when downloading keys for %s: %s %s",
                    server_name, type(e).__name__, str(e.message),
                )
                raise SynapseError(
                    502,
                    "Error downloading keys for %s" % (server_name,),
                    Codes.UNAUTHORIZED,
                )
            except Exception as e:
                logger.exception(
                    "Got Exception when downloading keys for %s: %s %s",
                    server_name, type(e).__name__, str(e.message),
                )
                raise SynapseError(
                    401,
                    "No key for %s with id %s" % (server_name, key_ids),
                    Codes.UNAUTHORIZED,
                )

            json_object = group_id_to_json[group.group_id]

            try:
                verify_signed_json(json_object, server_name, verify_key)
            except:
                raise SynapseError(
                    401,
                    "Invalid signature for server %s with key %s:%s" % (
                        server_name, verify_key.alg, verify_key.version
                    ),
                    Codes.UNAUTHORIZED,
                )

        server_to_deferred = {
            server_name: defer.Deferred()
            for server_name, _ in server_and_json
        }

        # We want to wait for any previous lookups to complete before
        # proceeding.
        wait_on_deferred = self.wait_for_previous_lookups(
            [server_name for server_name, _ in server_and_json],
            server_to_deferred,
        )

        # Actually start fetching keys.
        wait_on_deferred.addBoth(
            lambda _: self.get_server_verify_keys(group_id_to_group, deferreds)
        )

        # When we've finished fetching all the keys for a given server_name,
        # resolve the deferred passed to `wait_for_previous_lookups` so that
        # any lookups waiting will proceed.
        server_to_gids = {}

        def remove_deferreds(res, server_name, group_id):
            server_to_gids[server_name].discard(group_id)
            if not server_to_gids[server_name]:
                d = server_to_deferred.pop(server_name, None)
                if d:
                    d.callback(None)
            return res

        for g_id, deferred in deferreds.items():
            server_name = group_id_to_group[g_id].server_name
            server_to_gids.setdefault(server_name, set()).add(g_id)
            deferred.addBoth(remove_deferreds, server_name, g_id)

        # Pass those keys to handle_key_deferred so that the json object
        # signatures can be verified
        return [
            handle_key_deferred(
                group_id_to_group[g_id],
                deferreds[g_id],
            )
            for g_id in group_ids
        ]

    @defer.inlineCallbacks
    def wait_for_previous_lookups(self, server_names, server_to_deferred):
        """Waits for any previous key lookups for the given servers to finish.

        Args:
            server_names (list): list of server_names we want to lookup
            server_to_deferred (dict): server_name to deferred which gets
                resolved once we've finished looking up keys for that server
        """
        while True:
            wait_on = [
                self.key_downloads[server_name]
                for server_name in server_names
                if server_name in self.key_downloads
            ]
            if wait_on:
                yield defer.DeferredList(wait_on)
            else:
                break

        for server_name, deferred in server_to_deferred.items():
            d = ObservableDeferred(deferred)
            self.key_downloads[server_name] = d

            def rm(r, server_name):
                self.key_downloads.pop(server_name, None)
                return r

            d.addBoth(rm, server_name)

    def get_server_verify_keys(self, group_id_to_group, group_id_to_deferred):
        """Takes a dict of KeyGroups and tries to find at least one key for
        each group.
        """

        # These are functions that produce keys given a list of key ids
        key_fetch_fns = (
            self.get_keys_from_store,  # First try the local store
            self.get_keys_from_perspectives,  # Then try via perspectives
            self.get_keys_from_server,  # Then try directly
        )

        @defer.inlineCallbacks
        def do_iterations():
            merged_results = {}

            missing_keys = {
                group.server_name: set(group.key_ids)
                for group in group_id_to_group.values()
            }

            for fn in key_fetch_fns:
                results = yield fn(missing_keys.items())
                merged_results.update(results)

                # We now need to figure out which groups we have keys for
                # and which we don't
                missing_groups = {}
                for group in group_id_to_group.values():
                    for key_id in group.key_ids:
                        if key_id in merged_results[group.server_name]:
                            group_id_to_deferred[group.group_id].callback((
                                group.group_id,
                                group.server_name,
                                key_id,
                                merged_results[group.server_name][key_id],
                            ))
                            break
                    else:
                        missing_groups.setdefault(
                            group.server_name, []
                        ).append(group)

                if not missing_groups:
                    break

                missing_keys = {
                    server_name: set(
                        key_id for group in groups for key_id in group.key_ids
                    )
                    for server_name, groups in missing_groups.items()
                }

            for group in missing_groups.values():
                group_id_to_deferred[group.group_id].errback(SynapseError(
                    401,
                    "No key for %s with id %s" % (
                        group.server_name, group.key_ids,
                    ),
                    Codes.UNAUTHORIZED,
                ))

        def on_err(err):
            for deferred in group_id_to_deferred.values():
                if not deferred.called:
                    deferred.errback(err)

        do_iterations().addErrback(on_err)

        return group_id_to_deferred

    @defer.inlineCallbacks
    def get_keys_from_store(self, server_name_and_key_ids):
        res = yield defer.gatherResults(
            [
                self.store.get_server_verify_keys(
                    server_name, key_ids
                ).addCallback(lambda ks, server: (server, ks), server_name)
                for server_name, key_ids in server_name_and_key_ids
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError)

        defer.returnValue(dict(res))

    @defer.inlineCallbacks
    def get_keys_from_perspectives(self, server_name_and_key_ids):
        @defer.inlineCallbacks
        def get_key(perspective_name, perspective_keys):
            try:
                result = yield self.get_server_verify_key_v2_indirect(
                    server_name_and_key_ids, perspective_name, perspective_keys
                )
                defer.returnValue(result)
            except Exception as e:
                logger.exception(
                    "Unable to get key from %r: %s %s",
                    perspective_name,
                    type(e).__name__, str(e.message),
                )
                defer.returnValue({})

        results = yield defer.gatherResults(
            [
                get_key(p_name, p_keys)
                for p_name, p_keys in self.perspective_servers.items()
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError)

        union_of_keys = {}
        for result in results:
            for server_name, keys in result.items():
                union_of_keys.setdefault(server_name, {}).update(keys)

        defer.returnValue(union_of_keys)

    @defer.inlineCallbacks
    def get_keys_from_server(self, server_name_and_key_ids):
        @defer.inlineCallbacks
        def get_key(server_name, key_ids):
            limiter = yield get_retry_limiter(
                server_name,
                self.clock,
                self.store,
            )
            with limiter:
                keys = None
                try:
                    keys = yield self.get_server_verify_key_v2_direct(
                        server_name, key_ids
                    )
                except Exception as e:
                    logger.info(
                        "Unable to getting key %r for %r directly: %s %s",
                        key_ids, server_name,
                        type(e).__name__, str(e.message),
                    )

                if not keys:
                    keys = yield self.get_server_verify_key_v1_direct(
                        server_name, key_ids
                    )

                    keys = {server_name: keys}

            defer.returnValue(keys)

        results = yield defer.gatherResults(
            [
                get_key(server_name, key_ids)
                for server_name, key_ids in server_name_and_key_ids
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError)

        merged = {}
        for result in results:
            merged.update(result)

        defer.returnValue({
            server_name: keys
            for server_name, keys in merged.items()
            if keys
        })

    @defer.inlineCallbacks
    def get_server_verify_key_v2_indirect(self, server_names_and_key_ids,
                                          perspective_name,
                                          perspective_keys):
        limiter = yield get_retry_limiter(
            perspective_name, self.clock, self.store
        )

        with limiter:
            # TODO(mark): Set the minimum_valid_until_ts to that needed by
            # the events being validated or the current time if validating
            # an incoming request.
            query_response = yield self.client.post_json(
                destination=perspective_name,
                path=b"/_matrix/key/v2/query",
                data={
                    u"server_keys": {
                        server_name: {
                            key_id: {
                                u"minimum_valid_until_ts": 0
                            } for key_id in key_ids
                        }
                        for server_name, key_ids in server_names_and_key_ids
                    }
                },
            )

        keys = {}

        responses = query_response["server_keys"]

        for response in responses:
            if (u"signatures" not in response
                    or perspective_name not in response[u"signatures"]):
                raise ValueError(
                    "Key response not signed by perspective server"
                    " %r" % (perspective_name,)
                )

            verified = False
            for key_id in response[u"signatures"][perspective_name]:
                if key_id in perspective_keys:
                    verify_signed_json(
                        response,
                        perspective_name,
                        perspective_keys[key_id]
                    )
                    verified = True

            if not verified:
                logging.info(
                    "Response from perspective server %r not signed with a"
                    " known key, signed with: %r, known keys: %r",
                    perspective_name,
                    list(response[u"signatures"][perspective_name]),
                    list(perspective_keys)
                )
                raise ValueError(
                    "Response not signed with a known key for perspective"
                    " server %r" % (perspective_name,)
                )

            processed_response = yield self.process_v2_response(
                perspective_name, response
            )

            for server_name, response_keys in processed_response.items():
                keys.setdefault(server_name, {}).update(response_keys)

        yield defer.gatherResults(
            [
                self.store_keys(
                    server_name=server_name,
                    from_server=perspective_name,
                    verify_keys=response_keys,
                )
                for server_name, response_keys in keys.items()
            ],
            consumeErrors=True
        ).addErrback(unwrapFirstError)

        defer.returnValue(keys)

    @defer.inlineCallbacks
    def get_server_verify_key_v2_direct(self, server_name, key_ids):
        keys = {}

        for requested_key_id in key_ids:
            if requested_key_id in keys:
                continue

            (response, tls_certificate) = yield fetch_server_key(
                server_name, self.hs.tls_context_factory,
                path=(b"/_matrix/key/v2/server/%s" % (
                    urllib.quote(requested_key_id),
                )).encode("ascii"),
            )

            if (u"signatures" not in response
                    or server_name not in response[u"signatures"]):
                raise ValueError("Key response not signed by remote server")

            if "tls_fingerprints" not in response:
                raise ValueError("Key response missing TLS fingerprints")

            certificate_bytes = crypto.dump_certificate(
                crypto.FILETYPE_ASN1, tls_certificate
            )
            sha256_fingerprint = hashlib.sha256(certificate_bytes).digest()
            sha256_fingerprint_b64 = encode_base64(sha256_fingerprint)

            response_sha256_fingerprints = set()
            for fingerprint in response[u"tls_fingerprints"]:
                if u"sha256" in fingerprint:
                    response_sha256_fingerprints.add(fingerprint[u"sha256"])

            if sha256_fingerprint_b64 not in response_sha256_fingerprints:
                raise ValueError("TLS certificate not allowed by fingerprints")

            response_keys = yield self.process_v2_response(
                from_server=server_name,
                requested_ids=[requested_key_id],
                response_json=response,
            )

            keys.update(response_keys)

        yield defer.gatherResults(
            [
                self.store_keys(
                    server_name=key_server_name,
                    from_server=server_name,
                    verify_keys=verify_keys,
                )
                for key_server_name, verify_keys in keys.items()
            ],
            consumeErrors=True
        ).addErrback(unwrapFirstError)

        defer.returnValue(keys)

    @defer.inlineCallbacks
    def process_v2_response(self, from_server, response_json,
                            requested_ids=[]):
        time_now_ms = self.clock.time_msec()
        response_keys = {}
        verify_keys = {}
        for key_id, key_data in response_json["verify_keys"].items():
            if is_signing_algorithm_supported(key_id):
                key_base64 = key_data["key"]
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_key.time_added = time_now_ms
                verify_keys[key_id] = verify_key

        old_verify_keys = {}
        for key_id, key_data in response_json["old_verify_keys"].items():
            if is_signing_algorithm_supported(key_id):
                key_base64 = key_data["key"]
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_key.expired = key_data["expired_ts"]
                verify_key.time_added = time_now_ms
                old_verify_keys[key_id] = verify_key

        results = {}
        server_name = response_json["server_name"]
        for key_id in response_json["signatures"].get(server_name, {}):
            if key_id not in response_json["verify_keys"]:
                raise ValueError(
                    "Key response must include verification keys for all"
                    " signatures"
                )
            if key_id in verify_keys:
                verify_signed_json(
                    response_json,
                    server_name,
                    verify_keys[key_id]
                )

        signed_key_json = sign_json(
            response_json,
            self.config.server_name,
            self.config.signing_key[0],
        )

        signed_key_json_bytes = encode_canonical_json(signed_key_json)
        ts_valid_until_ms = signed_key_json[u"valid_until_ts"]

        updated_key_ids = set(requested_ids)
        updated_key_ids.update(verify_keys)
        updated_key_ids.update(old_verify_keys)

        response_keys.update(verify_keys)
        response_keys.update(old_verify_keys)

        yield defer.gatherResults(
            [
                self.store.store_server_keys_json(
                    server_name=server_name,
                    key_id=key_id,
                    from_server=server_name,
                    ts_now_ms=time_now_ms,
                    ts_expires_ms=ts_valid_until_ms,
                    key_json_bytes=signed_key_json_bytes,
                )
                for key_id in updated_key_ids
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError)

        results[server_name] = response_keys

        defer.returnValue(results)

    @defer.inlineCallbacks
    def get_server_verify_key_v1_direct(self, server_name, key_ids):
        """Finds a verification key for the server with one of the key ids.
        Args:
            server_name (str): The name of the server to fetch a key for.
            keys_ids (list of str): The key_ids to check for.
        """

        # Try to fetch the key from the remote server.

        (response, tls_certificate) = yield fetch_server_key(
            server_name, self.hs.tls_context_factory
        )

        # Check the response.

        x509_certificate_bytes = crypto.dump_certificate(
            crypto.FILETYPE_ASN1, tls_certificate
        )

        if ("signatures" not in response
                or server_name not in response["signatures"]):
            raise ValueError("Key response not signed by remote server")

        if "tls_certificate" not in response:
            raise ValueError("Key response missing TLS certificate")

        tls_certificate_b64 = response["tls_certificate"]

        if encode_base64(x509_certificate_bytes) != tls_certificate_b64:
            raise ValueError("TLS certificate doesn't match")

        # Cache the result in the datastore.

        time_now_ms = self.clock.time_msec()

        verify_keys = {}
        for key_id, key_base64 in response["verify_keys"].items():
            if is_signing_algorithm_supported(key_id):
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_key.time_added = time_now_ms
                verify_keys[key_id] = verify_key

        for key_id in response["signatures"][server_name]:
            if key_id not in response["verify_keys"]:
                raise ValueError(
                    "Key response must include verification keys for all"
                    " signatures"
                )
            if key_id in verify_keys:
                verify_signed_json(
                    response,
                    server_name,
                    verify_keys[key_id]
                )

        yield self.store.store_server_certificate(
            server_name,
            server_name,
            time_now_ms,
            tls_certificate,
        )

        yield self.store_keys(
            server_name=server_name,
            from_server=server_name,
            verify_keys=verify_keys,
        )

        defer.returnValue(verify_keys)

    @defer.inlineCallbacks
    def store_keys(self, server_name, from_server, verify_keys):
        """Store a collection of verify keys for a given server
        Args:
            server_name(str): The name of the server the keys are for.
            from_server(str): The server the keys were downloaded from.
            verify_keys(dict): A mapping of key_id to VerifyKey.
        Returns:
            A deferred that completes when the keys are stored.
        """
        # TODO(markjh): Store whether the keys have expired.
        yield defer.gatherResults(
            [
                self.store.store_server_verify_key(
                    server_name, server_name, key.time_added, key
                )
                for key_id, key in verify_keys.items()
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError)
