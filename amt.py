#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os, subprocess

from oslo.config import cfg

from ironic.common import exception
from ironic.common import states
from ironic.common import utils
from ironic.conductor import task_manager
from ironic.drivers import base
from ironic.drivers.modules import pxe
from ironic.openstack.common import log as logging
from ironic.openstack.common import processutils

CONF = cfg.CONF

LOG = logging.getLogger(__name__)

class AMTCommandFailed(Exception):
    pass

def _run_amt(node, cmd):
    info = node.get('driver_info', {})
    password = info.get('amt_password')
    if not password:
        raise exception.InvalidParameterValue('No amt_password given')
    host = info.get('amt_address')
    if not host:
        raise exception.InvalidParameterValue('No amt_address given')
    process = subprocess.Popen(['amttool', host, cmd], env={'AMT_PASSWORD': password}, stdout=subprocess.PIPE, stdin=subprocess.PIPE)
    out, err = process.communicate("y\n")
    if err:
        raise AMTCommandFailed('AMT command %s on %s failed: %s' % (cmd, host, err))
    return out


class AMTPower(base.PowerInterface):
    """AMT Power Interface.

    Does stuff.
    """
    
    def validate(self, task, node):
        """Check that the node's 'driver_info' is valid.

        Check that the node's 'driver_info' contains the requisite fields
        and that an AMT connection to the node can be established.

        :param task: a task from TaskManager.
        :param node: Single node object.
        :raises: InvalidParameterValue if any connection parameters are
            incorrect or if ssh failed to connect to the node.
        """
        try:
            _run_amt(node, 'info')
        except AMTCommandFailed as e:
            raise exception.InvalidParameterValue(_("AMT connection cannot"
                                                    " be established: %s") % e)

    def get_power_state(self, task, node):
        """Get the current power state.

        Poll the host for the current power state of the node.

        :param task: An instance of `ironic.manager.task_manager.TaskManager`.
        :param node: A single node.

        :returns: power state. One of :class:`ironic.common.states`.
        :raises: InvalidParameterValue if any connection parameters are
            incorrect.
        :raises: NodeNotFound.
        :raises: AMTCommandFailed on an error from AMT.
        """
        if 'Powerstate:   S0' in _run_amt(node, 'info'):
            return states.POWER_ON
        else:
            return states.POWER_OFF

    @task_manager.require_exclusive_lock
    def set_power_state(self, task, node, pstate):
        """Turn the power on or off.

        Set the power state of a node.

        :param task: An instance of `ironic.manager.task_manager.TaskManager`.
        :param node: A single node.
        :param pstate: Either POWER_ON or POWER_OFF from :class:
            `ironic.common.states`.

        :raises: InvalidParameterValue if any connection parameters are
            incorrect, or if the desired power state is invalid.
        :raises: NodeNotFound.
        :raises: PowerStateFailure if it failed to set power state to pstate.
        :raises: AMTCommandFailed on an error from AMT.
        """
        if pstate == states.POWER_ON:
            cmd = 'powerup'
        elif pstate == states.POWER_OFF:
            cmd = 'powerdown'
        else:
            raise exception.InvalidParameterValue(_("set_power_state called "
                    "with invalid power state %s.") % pstate)
        result = _run_amt(node, cmd)

        if not 'pt_status: success' in result:
            raise exception.PowerStateFailure(pstate=pstate)

    @task_manager.require_exclusive_lock
    def reboot(self, task, node):
        """Cycles the power to a node.

        Power cycles a node.

        :param task: An instance of `ironic.manager.task_manager.TaskManager`.
        :param node: A single node.

        :raises: InvalidParameterValue if any connection parameters are
            incorrect.
        :raises: NodeNotFound.
        :raises: PowerStateFailure if it failed to set power state to POWER_ON.
        :raises: AMTCommandFailed on an error from AMT.
        """
        result = _run_amt(node, 'powercycle')

        if not 'pt_status: success' in result:
            return self.set_power_state(task, node, states.POWER_ON)

    def get_properties(self):
        return {'amt_address': 'IP address of AMT endpoint. Required.',
                'amt_password': 'AMT password. Required.'}

class PXEAndAMTDriver(base.BaseDriver):
    def __init__(self):
        self.power = AMTPower()
        self.deploy = pxe.PXEDeploy()
        self.vendor = pxe.VendorPassthru()
