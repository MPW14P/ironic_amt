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

from ironic.common import boot_devices
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

REQUIRED_PROPERTIES = {'amt_address': 'IP address of AMT endpoint. Required.',
                       'amt_password': 'AMT password. Required.'}

_BOOT_DEVICES_MAP = {
    boot_devices.DISK: 'hd',
    boot_devices.PXE: 'pxe',
    boot_devices.CDROM: 'cd',
    boot_devices.SAFE: 'hdsafe'
}
DEFAULT_BOOT_DEVICE = boot_devices.DISK

class AMTCommandFailed(Exception):
    pass

def _run_amt(node, cmd):
    info = node.driver_info or {}
    missing_info = [key for key in REQUIRED_PROPERTIES if not info.get(key)]
    if missing_info:
        raise exception.MissingParameterValue(_("AMTPowerDriver requires the following to be set: %s.")
            % missing_info)

    password = info.get('amt_password')
    host = info.get('amt_address')
    process = subprocess.Popen(['amttool', host, cmd], env={'AMT_PASSWORD': password}, stdout=subprocess.PIPE, stdin=subprocess.PIPE)
    out, err = process.communicate("y\n")
    if err:
        raise AMTCommandFailed('AMT command %s on %s failed: %s' % (cmd, host, err))
    return out


class AMTPower(base.PowerInterface):
    """AMT Power Interface.

    Does stuff.
    """
    
    def validate(self, task):
        """Check that the node's 'driver_info' is valid.

        Check that the node's 'driver_info' contains the requisite fields
        and that an AMT connection to the node can be established.

        :param task: a task from TaskManager.
        :param node: Single node object.
        :raises: InvalidParameterValue if any connection parameters are
            incorrect or if ssh failed to connect to the node.
        """
        try:
            _run_amt(task.node, 'info')
        except AMTCommandFailed as e:
            raise exception.InvalidParameterValue(_("AMT connection cannot"
                                                    " be established: %s") % e)

    def get_power_state(self, task):
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
        if 'Powerstate:   S0' in _run_amt(task.node, 'info'):
            return states.POWER_ON
        else:
            return states.POWER_OFF

    @task_manager.require_exclusive_lock
    def set_power_state(self, task, pstate):
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
        LOG.info('Setting power state.')
        LOG.info('Selected boot device: ' % _BOOT_DEVICES_MAP[_get_boot_device(task.node)])
        if pstate == states.POWER_ON:
            cmd = 'powerup ' + _BOOT_DEVICES_MAP[_get_boot_device(task.node)]
        elif pstate == states.POWER_OFF:
            cmd = 'powerdown'
        else:
            raise exception.InvalidParameterValue(_("set_power_state called "
                    "with invalid power state %s.") % pstate)
        result = _run_amt(task.node, cmd)

        if not 'pt_status: success' in result:
            raise exception.PowerStateFailure(pstate=pstate)

    @task_manager.require_exclusive_lock
    def reboot(self, task):
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
        result = _run_amt(task.node, 'powercycle ' + _BOOT_DEVICES_MAP[_get_boot_device(task.node)])

        if not 'pt_status: success' in result:
            return self.set_power_state(task, states.POWER_ON)

    def get_properties(self):
        return REQUIRED_PROPERTIES

def _get_boot_device(node):
    info = node.driver_info or {}
    if 'amt_boot_device' in info:
        boot_device = info['amt_boot_device']
    else:
        boot_device = DEFAULT_BOOT_DEVICE
    return boot_device

class AMTManagement(base.ManagementInterface):
    def get_properties(self):
        return {}

    def validate(self, task):
        pass

    def get_supported_boot_devices(self):
        """Get a list of the supported boot devices.

        :returns: A list with the supported boot devices defined
                  in :mod:`ironic.common.boot_devices`.

        """
        return list(_BOOT_DEVICES_MAP.keys())

    @task_manager.require_exclusive_lock
    def set_boot_device(self, task, device, persistent=False):
        """Set the boot device for the task's node.

        Set the boot device to use on next reboot of the node.

        :param task: a task from TaskManager.
        :param device: the boot device, one of
                       :mod:`ironic.common.boot_devices`.
        :param persistent: Boolean value. True if the boot device will
                           persist to all future boots, False if not.
                           Default: False. Ignored by this driver.
        :raises: InvalidParameterValue if an invalid boot device is
                 specified or if any connection parameters are incorrect.
        :raises: MissingParameterValue if a required parameter is missing
        :raises: AMTCommandFailed on an error from ssh.
        :raises: NotImplementedError if the virt_type does not support
            setting the boot device.

        """
        if device not in self.get_supported_boot_devices():
             raise exception.InvalidParameterValue(_(
                "Invalid boot device %s specified.") % device)

        # Seperate variable needed to mark model data as modified....
        info = task.node.driver_info
        info['amt_boot_device'] = device
        task.node.driver_info = info
        task.node.save()

    def get_boot_device(self, task):
        """Get the current boot device for the task's node.

        Provides the current boot device of the node. Be aware that not
        all drivers support this.

        :param task: a task from TaskManager.
        :raises: InvalidParameterValue if any connection parameters are
            incorrect.
        :raises: MissingParameterValue if a required parameter is missing
        :raises: AMTCommandFailed on an error from ssh.
        :returns: a dictionary containing:

            :boot_device: the boot device, one of
                :mod:`ironic.common.boot_devices` or None if it is unknown.
            :persistent: Whether the boot device will persist to all
                future boots or not, None if it is unknown.

        """
        
        response = {'boot_device': _get_boot_device(task.node), 'persistent': True}
        return response

    def get_sensors_data(self, task):
        """Get sensors data.

        Not implemented by this driver.

        :param task: a TaskManager instance.

        """
        raise NotImplementedError()

class PXEAndAMTDriver(base.BaseDriver):
    def __init__(self):
        self.power = AMTPower()
        self.management = AMTManagement()
        self.deploy = pxe.PXEDeploy()
        self.vendor = pxe.VendorPassthru()
