#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# (c) 2015, René Moser <mail@renemoser.net>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible. If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = '''
---
module: cs_account
short_description: Manages accounts on Apache CloudStack based clouds.
description:
    - Create, disable, lock, enable and remove accounts.
version_added: '2.0'
author: "René Moser (@resmo)"
options:
  name:
    description:
      - Name of account.
    required: true
  username:
    description:
      - Username of the user to be created if account did not exist.
      - Required on C(state=present).
    required: false
    default: null
  password:
    description:
      - Password of the user to be created if account did not exist.
      - Required on C(state=present).
    required: false
    default: null
  first_name:
    description:
      - First name of the user to be created if account did not exist.
      - Required on C(state=present).
    required: false
    default: null
  last_name:
    description:
      - Last name of the user to be created if account did not exist.
      - Required on C(state=present).
    required: false
    default: null
  email:
    description:
      - Email of the user to be created if account did not exist.
      - Required on C(state=present).
    required: false
    default: null
  timezone:
    description:
      - Timezone of the user to be created if account did not exist.
    required: false
    default: null
  network_domain:
    description:
      - Network domain of the account.
    required: false
    default: null
  account_type:
    description:
      - Type of the account.
    required: false
    default: 'user'
    choices: [ 'user', 'root_admin', 'domain_admin' ]
  domain:
    description:
      - Domain the account is related to.
    required: false
    default: 'ROOT'
  state:
    description:
      - State of the account.
      - C(unlocked) is an alias for C(enabled).
    required: false
    default: 'present'
    choices: [ 'present', 'absent', 'enabled', 'disabled', 'locked', 'unlocked' ]
  poll_async:
    description:
      - Poll async jobs until job has finished.
    required: false
    default: true
extends_documentation_fragment: cloudstack
'''

EXAMPLES = '''
# create an account in domain 'CUSTOMERS'
local_action:
  module: cs_account
  name: customer_xy
  username: customer_xy
  password: S3Cur3
  last_name: Doe
  first_name: John
  email: john.doe@example.com
  domain: CUSTOMERS

# Lock an existing account in domain 'CUSTOMERS'
local_action:
  module: cs_account
  name: customer_xy
  domain: CUSTOMERS
  state: locked

# Disable an existing account in domain 'CUSTOMERS'
local_action:
  module: cs_account
  name: customer_xy
  domain: CUSTOMERS
  state: disabled

# Enable an existing account in domain 'CUSTOMERS'
local_action:
  module: cs_account
  name: customer_xy
  domain: CUSTOMERS
  state: enabled

# Remove an account in domain 'CUSTOMERS'
local_action:
  module: cs_account
  name: customer_xy
  domain: CUSTOMERS
  state: absent
'''

RETURN = '''
---
id:
  description: UUID of the account.
  returned: success
  type: string
  sample: 87b1e0ce-4e01-11e4-bb66-0050569e64b8
name:
  description: Name of the account.
  returned: success
  type: string
  sample: linus@example.com
account_type:
  description: Type of the account.
  returned: success
  type: string
  sample: user
state:
  description: State of the account.
  returned: success
  type: string
  sample: enabled
network_domain:
  description: Network domain of the account.
  returned: success
  type: string
  sample: example.local
domain:
  description: Domain the account is related.
  returned: success
  type: string
  sample: ROOT
'''

# import cloudstack common
import os
import time
from ansible.module_utils.six import iteritems

try:
    from cs import CloudStack, CloudStackException, read_config
    has_lib_cs = True
except ImportError:
    has_lib_cs = False

CS_HYPERVISORS = [
    "KVM", "kvm",
    "VMware", "vmware",
    "BareMetal", "baremetal",
    "XenServer", "xenserver",
    "LXC", "lxc",
    "HyperV", "hyperv",
    "UCS", "ucs",
    "OVM", "ovm",
    "Simulator", "simulator",
    ]

def cs_argument_spec():
    return dict(
        api_key = dict(default=None),
        api_secret = dict(default=None, no_log=True),
        api_url = dict(default=None),
        api_http_method = dict(choices=['get', 'post'], default='get'),
        api_timeout = dict(type='int', default=10),
        api_region = dict(default='cloudstack'),
    )

def cs_required_together():
    return [['api_key', 'api_secret', 'api_url']]

class AnsibleCloudStack(object):

    def __init__(self, module):
        if not has_lib_cs:
            module.fail_json(msg="python library cs required: pip install cs")

        self.result = {
            'changed': False,
            'diff' : {
                'before': dict(),
                'after': dict()
            }
        }

        # Common returns, will be merged with self.returns
        # search_for_key: replace_with_key
        self.common_returns = {
            'id':           'id',
            'name':         'name',
            'created':      'created',
            'zonename':     'zone',
            'state':        'state',
            'project':      'project',
            'account':      'account',
            'domain':       'domain',
            'displaytext':  'display_text',
            'displayname':  'display_name',
            'description':  'description',
        }

        # Init returns dict for use in subclasses
        self.returns = {}
        # these values will be casted to int
        self.returns_to_int = {}
        # these keys will be compared case sensitive in self.has_changed()
        self.case_sensitive_keys = [
            'id',
            'displaytext',
            'displayname',
            'description',
        ]

        self.module = module
        self._connect()

        # Helper for VPCs
        self._vpc_networks_ids = None

        self.domain = None
        self.account = None
        self.project = None
        self.ip_address = None
        self.network = None
        self.vpc = None
        self.zone = None
        self.vm = None
        self.vm_default_nic = None
        self.os_type = None
        self.hypervisor = None
        self.capabilities = None


    def _connect(self):
        api_key = self.module.params.get('api_key')
        api_secret = self.module.params.get('api_secret')
        api_url = self.module.params.get('api_url')
        api_http_method = self.module.params.get('api_http_method')
        api_timeout = self.module.params.get('api_timeout')

        if api_key and api_secret and api_url:
            self.cs = CloudStack(
                endpoint=api_url,
                key=api_key,
                secret=api_secret,
                timeout=api_timeout,
                method=api_http_method
                )
        else:
            api_region = self.module.params.get('api_region', 'cloudstack')
            self.cs = CloudStack(**read_config(api_region))


    def get_or_fallback(self, key=None, fallback_key=None):
        value = self.module.params.get(key)
        if not value:
            value = self.module.params.get(fallback_key)
        return value


    # TODO: for backward compatibility only, remove if not used anymore
    def _has_changed(self, want_dict, current_dict, only_keys=None):
        return self.has_changed(want_dict=want_dict, current_dict=current_dict, only_keys=only_keys)


    def has_changed(self, want_dict, current_dict, only_keys=None):
        result = False
        for key, value in want_dict.iteritems():

            # Optionally limit by a list of keys
            if only_keys and key not in only_keys:
                continue

            # Skip None values
            if value is None:
                continue

            if key in current_dict:
                if isinstance(value, (int, float, long, complex)):
                    # ensure we compare the same type
                    if isinstance(value, int):
                        current_dict[key] = int(current_dict[key])
                    elif isinstance(value, float):
                        current_dict[key] = float(current_dict[key])
                    elif isinstance(value, long):
                        current_dict[key] = long(current_dict[key])
                    elif isinstance(value, complex):
                        current_dict[key] = complex(current_dict[key])

                    if value != current_dict[key]:
                        self.result['diff']['before'][key] = current_dict[key]
                        self.result['diff']['after'][key] = value
                        result = True
                else:
                    if self.case_sensitive_keys and key in self.case_sensitive_keys:
                        if value != current_dict[key].encode('utf-8'):
                            self.result['diff']['before'][key] = current_dict[key].encode('utf-8')
                            self.result['diff']['after'][key] = value
                            result = True

                    # Test for diff in case insensitive way
                    elif value.lower() != current_dict[key].encode('utf-8').lower():
                        self.result['diff']['before'][key] = current_dict[key].encode('utf-8')
                        self.result['diff']['after'][key] = value
                        result = True
            else:
                self.result['diff']['before'][key] = None
                self.result['diff']['after'][key] = value
                result = True
        return result


    def _get_by_key(self, key=None, my_dict=None):
        if my_dict is None:
            my_dict = {}
        if key:
            if key in my_dict:
                return my_dict[key]
            self.module.fail_json(msg="Something went wrong: %s not found" % key)
        return my_dict


    def get_vpc(self, key=None):
        """Return a VPC dictionary or the value of given key of."""
        if self.vpc:
            return self._get_by_key(key, self.vpc)

        vpc = self.module.params.get('vpc')
        if not vpc:
            vpc = os.environ.get('CLOUDSTACK_VPC')
        if not vpc:
            return None

        args = {
            'account': self.get_account(key='name'),
            'domainid': self.get_domain(key='id'),
            'projectid': self.get_project(key='id'),
            'zoneid': self.get_zone(key='id'),
        }
        vpcs = self.cs.listVPCs(**args)
        if not vpcs:
            self.module.fail_json(msg="No VPCs available.")

        for v in vpcs['vpc']:
            if vpc in [v['displaytext'], v['name'], v['id']]:
                self.vpc = v
                return self._get_by_key(key, self.vpc)
        self.module.fail_json(msg="VPC '%s' not found" % vpc)


    def is_vm_in_vpc(self, vm):
        for n in vm.get('nic'):
            if n.get('isdefault', False):
                return self.is_vpc_network(network_id=n['networkid'])
        self.module.fail_json(msg="VM has no default nic")


    def is_vpc_network(self, network_id):
        """Returns True if network is in VPC."""
        # This is an efficient way to query a lot of networks at a time
        if self._vpc_networks_ids is None:
            args = {
                'account': self.get_account(key='name'),
                'domainid': self.get_domain(key='id'),
                'projectid': self.get_project(key='id'),
                'zoneid': self.get_zone(key='id'),
            }
            vpcs = self.cs.listVPCs(**args)
            self._vpc_networks_ids = []
            if vpcs:
                for vpc in vpcs['vpc']:
                    for n in vpc.get('network',[]):
                        self._vpc_networks_ids.append(n['id'])
        return network_id in self._vpc_networks_ids


    def get_network(self, key=None):
        """Return a network dictionary or the value of given key of."""
        if self.network:
            return self._get_by_key(key, self.network)

        network = self.module.params.get('network')
        if not network:
            return None

        args = {
            'account': self.get_account(key='name'),
            'domainid': self.get_domain(key='id'),
            'projectid': self.get_project(key='id'),
            'zoneid': self.get_zone(key='id'),
            'vpcid': self.get_vpc(key='id')
        }
        networks = self.cs.listNetworks(**args)
        if not networks:
            self.module.fail_json(msg="No networks available.")

        for n in networks['network']:
            # ignore any VPC network if vpc param is not given
            if 'vpcid' in n and not self.get_vpc(key='id'):
                continue
            if network in [n['displaytext'], n['name'], n['id']]:
                self.network = n
                return self._get_by_key(key, self.network)
        self.module.fail_json(msg="Network '%s' not found" % network)


    def get_project(self, key=None):
        if self.project:
            return self._get_by_key(key, self.project)

        project = self.module.params.get('project')
        if not project:
            project = os.environ.get('CLOUDSTACK_PROJECT')
        if not project:
            return None
        args = {}
        args['account'] = self.get_account(key='name')
        args['domainid'] = self.get_domain(key='id')
        projects = self.cs.listProjects(**args)
        if projects:
            for p in projects['project']:
                if project.lower() in [ p['name'].lower(), p['id'] ]:
                    self.project = p
                    return self._get_by_key(key, self.project)
        self.module.fail_json(msg="project '%s' not found" % project)


    def get_ip_address(self, key=None):
        if self.ip_address:
            return self._get_by_key(key, self.ip_address)

        ip_address = self.module.params.get('ip_address')
        if not ip_address:
            self.module.fail_json(msg="IP address param 'ip_address' is required")

        args = {
            'ipaddress': ip_address,
            'account': self.get_account(key='name'),
            'domainid': self.get_domain(key='id'),
            'projectid': self.get_project(key='id'),
            'vpcid': self.get_vpc(key='id'),
        }
        ip_addresses = self.cs.listPublicIpAddresses(**args)

        if not ip_addresses:
            self.module.fail_json(msg="IP address '%s' not found" % args['ipaddress'])

        self.ip_address = ip_addresses['publicipaddress'][0]
        return self._get_by_key(key, self.ip_address)


    def get_vm_guest_ip(self):
        vm_guest_ip = self.module.params.get('vm_guest_ip')
        default_nic = self.get_vm_default_nic()

        if not vm_guest_ip:
            return default_nic['ipaddress']

        for secondary_ip in default_nic['secondaryip']:
            if vm_guest_ip == secondary_ip['ipaddress']:
                return vm_guest_ip
        self.module.fail_json(msg="Secondary IP '%s' not assigned to VM" % vm_guest_ip)


    def get_vm_default_nic(self):
        if self.vm_default_nic:
            return self.vm_default_nic

        nics = self.cs.listNics(virtualmachineid=self.get_vm(key='id'))
        if nics:
            for n in nics['nic']:
                if n['isdefault']:
                    self.vm_default_nic = n
                    return self.vm_default_nic
        self.module.fail_json(msg="No default IP address of VM '%s' found" % self.module.params.get('vm'))


    def get_vm(self, key=None):
        if self.vm:
            return self._get_by_key(key, self.vm)

        vm = self.module.params.get('vm')
        if not vm:
            self.module.fail_json(msg="Virtual machine param 'vm' is required")

        vpc_id = self.get_vpc(key='id')

        args = {
            'account': self.get_account(key='name'),
            'domainid': self.get_domain(key='id'),
            'projectid': self.get_project(key='id'),
            'zoneid': self.get_zone(key='id'),
            'vpcid': vpc_id,
        }
        vms = self.cs.listVirtualMachines(**args)
        if vms:
            for v in vms['virtualmachine']:
                # Due the limitation of the API, there is no easy way (yet) to get only those VMs
                # not belonging to a VPC.
                if not vpc_id and self.is_vm_in_vpc(vm=v):
                    continue
                if vm.lower() in [ v['name'].lower(), v['displayname'].lower(), v['id'] ]:
                    self.vm = v
                    return self._get_by_key(key, self.vm)
        self.module.fail_json(msg="Virtual machine '%s' not found" % vm)


    def get_zone(self, key=None):
        if self.zone:
            return self._get_by_key(key, self.zone)

        zone = self.module.params.get('zone')
        if not zone:
            zone = os.environ.get('CLOUDSTACK_ZONE')
        zones = self.cs.listZones()

        # use the first zone if no zone param given
        if not zone:
            self.zone = zones['zone'][0]
            return self._get_by_key(key, self.zone)

        if zones:
            for z in zones['zone']:
                if zone.lower() in [ z['name'].lower(), z['id'] ]:
                    self.zone = z
                    return self._get_by_key(key, self.zone)
        self.module.fail_json(msg="zone '%s' not found" % zone)


    def get_os_type(self, key=None):
        if self.os_type:
            return self._get_by_key(key, self.zone)

        os_type = self.module.params.get('os_type')
        if not os_type:
            return None

        os_types = self.cs.listOsTypes()
        if os_types:
            for o in os_types['ostype']:
                if os_type in [ o['description'], o['id'] ]:
                    self.os_type = o
                    return self._get_by_key(key, self.os_type)
        self.module.fail_json(msg="OS type '%s' not found" % os_type)


    def get_hypervisor(self):
        if self.hypervisor:
            return self.hypervisor

        hypervisor = self.module.params.get('hypervisor')
        hypervisors = self.cs.listHypervisors()

        # use the first hypervisor if no hypervisor param given
        if not hypervisor:
            self.hypervisor = hypervisors['hypervisor'][0]['name']
            return self.hypervisor

        for h in hypervisors['hypervisor']:
            if hypervisor.lower() == h['name'].lower():
                self.hypervisor = h['name']
                return self.hypervisor
        self.module.fail_json(msg="Hypervisor '%s' not found" % hypervisor)


    def get_account(self, key=None):
        if self.account:
            return self._get_by_key(key, self.account)

        account = self.module.params.get('account')
        if not account:
            account = os.environ.get('CLOUDSTACK_ACCOUNT')
        if not account:
            return None

        domain = self.module.params.get('domain')
        if not domain:
            self.module.fail_json(msg="Account must be specified with Domain")

        args = {}
        args['name'] = account
        args['domainid'] = self.get_domain(key='id')
        args['listall'] = True
        accounts = self.cs.listAccounts(**args)
        if accounts:
            self.account = accounts['account'][0]
            return self._get_by_key(key, self.account)
        self.module.fail_json(msg="Account '%s' not found" % account)


    def get_domain(self, key=None):
        if self.domain:
            return self._get_by_key(key, self.domain)

        domain = self.module.params.get('domain')
        if not domain:
            domain = os.environ.get('CLOUDSTACK_DOMAIN')
        if not domain:
            return None

        args = {}
        args['listall'] = True
        domains = self.cs.listDomains(**args)
        if domains:
            for d in domains['domain']:
                if d['path'].lower() in [ domain.lower(), "root/" + domain.lower(), "root" + domain.lower() ]:
                    self.domain = d
                    return self._get_by_key(key, self.domain)
        self.module.fail_json(msg="Domain '%s' not found" % domain)


    def get_tags(self, resource=None):
        existing_tags = []
        for tag in resource.get('tags',[]):
            existing_tags.append({'key': tag['key'], 'value': tag['value']})
        return existing_tags


    def _process_tags(self, resource, resource_type, tags, operation="create"):
        if tags:
            self.result['changed'] = True
            if not self.module.check_mode:
                args = {}
                args['resourceids']  = resource['id']
                args['resourcetype'] = resource_type
                args['tags']         = tags
                if operation == "create":
                    response = self.cs.createTags(**args)
                else:
                    response = self.cs.deleteTags(**args)
                self.poll_job(response)


    def _tags_that_should_exist_or_be_updated(self, resource, tags):
        existing_tags = self.get_tags(resource)
        return [tag for tag in tags if tag not in existing_tags]


    def _tags_that_should_not_exist(self, resource, tags):
        existing_tags = self.get_tags(resource)
        return [tag for tag in existing_tags if tag not in tags]


    def ensure_tags(self, resource, resource_type=None):
        if not resource_type or not resource:
            self.module.fail_json(msg="Error: Missing resource or resource_type for tags.")

        if 'tags' in resource:
            tags = self.module.params.get('tags')
            if tags is not None:
                self._process_tags(resource, resource_type, self._tags_that_should_not_exist(resource, tags), operation="delete")
                self._process_tags(resource, resource_type, self._tags_that_should_exist_or_be_updated(resource, tags))
                resource['tags'] = tags
        return resource


    def get_capabilities(self, key=None):
        if self.capabilities:
            return self._get_by_key(key, self.capabilities)
        capabilities = self.cs.listCapabilities()
        self.capabilities = capabilities['capability']
        return self._get_by_key(key, self.capabilities)


    # TODO: for backward compatibility only, remove if not used anymore
    def _poll_job(self, job=None, key=None):
        return self.poll_job(job=job, key=key)


    def poll_job(self, job=None, key=None):
        if 'jobid' in job:
            while True:
                res = self.cs.queryAsyncJobResult(jobid=job['jobid'])
                if res['jobstatus'] != 0 and 'jobresult' in res:
                    if 'errortext' in res['jobresult']:
                        self.module.fail_json(msg="Failed: '%s'" % res['jobresult']['errortext'])
                    if key and key in res['jobresult']:
                        job = res['jobresult'][key]
                    break
                time.sleep(2)
        return job


    def get_result(self, resource):
        if resource:
            returns = self.common_returns.copy()
            returns.update(self.returns)
            for search_key, return_key in returns.iteritems():
                if search_key in resource:
                    self.result[return_key] = resource[search_key]

            # Bad bad API does not always return int when it should.
            for search_key, return_key in self.returns_to_int.iteritems():
                if search_key in resource:
                    self.result[return_key] = int(resource[search_key])

            # Special handling for tags
            if 'tags' in resource:
                self.result['tags'] = []
                for tag in resource['tags']:
                    result_tag          = {}
                    result_tag['key']   = tag['key']
                    result_tag['value'] = tag['value']
                    self.result['tags'].append(result_tag)
        return self.result


class AnsibleCloudStackAccount(AnsibleCloudStack):

    def __init__(self, module):
        super(AnsibleCloudStackAccount, self).__init__(module)
        self.returns = {
            'networkdomain': 'network_domain',
        }
        self.account = None
        self.account_types = {
            'user':         0,
            'root_admin':   1,
            'domain_admin': 2,
        }

    def get_account_type(self):
        account_type = self.module.params.get('account_type')
        return self.account_types[account_type]

    def get_account(self):
        if not self.account:
            args = {
                'listall': True,
                'domainid': self.get_domain(key='id'),
            }
            accounts = self.cs.listAccounts(**args)
            if accounts:
                account_name = self.module.params.get('name')
                for a in accounts['account']:
                    if account_name == a['name']:
                        self.account = a
                        break

        return self.account

    def enable_account(self):
        account = self.get_account()
        if not account:
            account = self.present_account()

        if account['state'].lower() != 'enabled':
            self.result['changed'] = True
            args = {
                'id': account['id'],
                'account': self.module.params.get('name'),
                'domainid': self.get_domain(key='id')
            }
            if not self.module.check_mode:
                res = self.cs.enableAccount(**args)
                if 'errortext' in res:
                    self.module.fail_json(msg="Failed: '%s'" % res['errortext'])
                account = res['account']
        return account

    def lock_account(self):
        return self.lock_or_disable_account(lock=True)

    def disable_account(self):
        return self.lock_or_disable_account()

    def lock_or_disable_account(self, lock=False):
        account = self.get_account()
        if not account:
            account = self.present_account()

        # we need to enable the account to lock it.
        if lock and account['state'].lower() == 'disabled':
            account = self.enable_account()

        if (lock and account['state'].lower() != 'locked' or
                not lock and account['state'].lower() != 'disabled'):
            self.result['changed'] = True
            args = {
                'id': account['id'],
                'account': self.module.params.get('name'),
                'domainid': self.get_domain(key='id'),
                'lock': lock,
            }
            if not self.module.check_mode:
                account = self.cs.disableAccount(**args)

                if 'errortext' in account:
                    self.module.fail_json(msg="Failed: '%s'" % account['errortext'])

                poll_async = self.module.params.get('poll_async')
                if poll_async:
                    account = self.poll_job(account, 'account')
        return account

    def present_account(self):
        required_params = [
            'email',
            'username',
            'password',
            'first_name',
            'last_name',
        ]
        self.module.fail_on_missing_params(required_params=required_params)

        account = self.get_account()

        if not account:
            self.result['changed'] = True

            args = {
                'account': self.module.params.get('name'),
                'domainid': self.get_domain(key='id'),
                'accounttype': self.get_account_type(),
                'networkdomain': self.module.params.get('network_domain'),
                'username': self.module.params.get('username'),
                'password': self.module.params.get('password'),
                'firstname': self.module.params.get('first_name'),
                'lastname': self.module.params.get('last_name'),
                'email': self.module.params.get('email'),
                'timezone': self.module.params.get('timezone')
            }
            if not self.module.check_mode:
                res = self.cs.createAccount(**args)
                if 'errortext' in res:
                    self.module.fail_json(msg="Failed: '%s'" % res['errortext'])
                account = res['account']
        return account

    def absent_account(self):
        account = self.get_account()
        if account:
            self.result['changed'] = True

            if not self.module.check_mode:
                res = self.cs.deleteAccount(id=account['id'])

                if 'errortext' in res:
                    self.module.fail_json(msg="Failed: '%s'" % res['errortext'])

                poll_async = self.module.params.get('poll_async')
                if poll_async:
                    self.poll_job(res, 'account')
        return account

    def get_result(self, account):
        super(AnsibleCloudStackAccount, self).get_result(account)
        if account:
            if 'accounttype' in account:
                for key, value in self.account_types.items():
                    if value == account['accounttype']:
                        self.result['account_type'] = key
                        break
        return self.result


def main():
    argument_spec = cs_argument_spec()
    argument_spec.update(dict(
        name=dict(required=True),
        state=dict(choices=['present', 'absent', 'enabled', 'disabled', 'locked', 'unlocked'], default='present'),
        account_type=dict(choices=['user', 'root_admin', 'domain_admin'], default='user'),
        network_domain=dict(default=None),
        domain=dict(default='ROOT'),
        email=dict(default=None),
        first_name=dict(default=None),
        last_name=dict(default=None),
        username=dict(default=None),
        password=dict(default=None, no_log=True),
        timezone=dict(default=None),
        poll_async=dict(type='bool', default=True),
    ))

    module = AnsibleModule(
        argument_spec=argument_spec,
        required_together=cs_required_together(),
        supports_check_mode=True
    )

    try:
        acs_acc = AnsibleCloudStackAccount(module)

        state = module.params.get('state')

        if state in ['absent']:
            account = acs_acc.absent_account()

        elif state in ['enabled', 'unlocked']:
            account = acs_acc.enable_account()

        elif state in ['disabled']:
            account = acs_acc.disable_account()

        elif state in ['locked']:
            account = acs_acc.lock_account()

        else:
            account = acs_acc.present_account()

        result = acs_acc.get_result(account)

    except CloudStackException as e:
        module.fail_json(msg='CloudStackException: %s' % str(e))

    module.exit_json(**result)

# import module snippets
from ansible.module_utils.basic import *
if __name__ == '__main__':
    main()
