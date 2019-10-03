# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import fixtures
from oslo_db import exception as oslo_db_exc

from nova.compute import manager as compute_manager
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import fixtures as func_fixtures
from nova.tests.functional import integrated_helpers
from nova.tests.unit import fake_notifier
from nova.tests.unit.image import fake as fake_image
from nova.tests.unit import policy_fixture


class RescheduleBuildAvailabilityZoneUpCall(
        test.TestCase, integrated_helpers.InstanceHelperMixin):
    """This is a regression test for bug 1781286 which was introduced with
    a change in Pike to set the instance availability_zone in conductor
    once a host is selected from the scheduler. The regression in the initial
    server build case is when a reschedule is triggered, and the cell conductor
    does not have access to the API DB, it fails with a CantStartEngineError
    trying to connect to the API DB to get availability zone (aggregate) info
    about the alternate host selection.
    """
    def setUp(self):
        super(RescheduleBuildAvailabilityZoneUpCall, self).setUp()
        # Use the standard fixtures.
        self.useFixture(policy_fixture.RealPolicyFixture())
        self.useFixture(nova_fixtures.NeutronFixture(self))
        self.useFixture(func_fixtures.PlacementFixture())
        fake_image.stub_out_image_service(self)
        self.addCleanup(fake_image.FakeImageService_reset)
        # Start controller services.
        self.api = self.useFixture(nova_fixtures.OSAPIFixture(
            api_version='v2.1')).admin_api
        self.start_service('conductor')
        self.start_service('scheduler')
        # Start two computes with the fake reschedule driver.
        self.flags(compute_driver='fake.FakeRescheduleDriver')
        self.start_service('compute', host='host1')
        self.start_service('compute', host='host2')
        # Listen for notifications.
        fake_notifier.stub_notifier(self)
        self.addCleanup(fake_notifier.reset)

    def test_server_create_reschedule_blocked_az_up_call(self):
        # We need to stub out the call to get_host_availability_zone to blow
        # up once we have gone to the compute service. With the way our
        # RPC/DB fixtures are setup it's non-trivial to try and separate a
        # superconductor from a cell conductor so we can configure the cell
        # conductor from not having access to the API DB but that would be a
        # a nice thing to have at some point.
        original_bari = compute_manager.ComputeManager.build_and_run_instance

        def wrap_bari(*args, **kwargs):
            # Poison the AZ query to blow up as if the cell conductor does not
            # have access to the API DB.
            self.useFixture(
                fixtures.MockPatch(
                    'nova.objects.AggregateList.get_by_host',
                    side_effect=oslo_db_exc.CantStartEngineError))
            return original_bari(*args, **kwargs)

        self.stub_out('nova.compute.manager.ComputeManager.'
                      'build_and_run_instance', wrap_bari)
        server = self._build_minimal_create_server_request(
            self.api, 'test_server_create_reschedule_blocked_az_up_call')
        server = self.api.post_server({'server': server})
        # FIXME(mriedem): This is bug 1781286 where we reschedule from the
        # first failed host to conductor which will try to get the AZ for the
        # alternate host selection which will fail since it cannot access the
        # API DB.
        # Note that we have to wait for the notification before calling the API
        # to avoid a race where instance.host is not None and the API tries to
        # hit the AggregateList.get_by_host method we mocked to fail.
        fake_notifier.wait_for_versioned_notifications(
            'compute_task.build_instances.error')
        server = self._wait_for_server_parameter(
            self.api, server, {'status': 'ERROR',
                               'OS-EXT-SRV-ATTR:host': None,
                               'OS-EXT-STS:task_state': None})
        # Assert there is a fault injected on the server for the error we
        # expect.
        self.assertIn('CantStartEngineError', server['fault']['message'])
