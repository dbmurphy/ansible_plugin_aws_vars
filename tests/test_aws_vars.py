"""Tests for AWS Vars Plugin."""
import json
from unittest.mock import MagicMock, patch

import pytest
from ansible.inventory.host import Host

# Add BASE_PATH constant
BASE_PATH = "/aws_vars"

from ansible_plugin_aws_vars.plugins.vars.aws_vars import (
    VarsModule, PathComponents
)

@pytest.fixture
def vars_plugin():
    """Create a VarsModule instance with mocked AWS clients."""
    with patch('boto3.client') as mock_boto:
        plugin = VarsModule()
        # Mock both SSM and Secrets Manager clients
        plugin.ssm = MagicMock()
        plugin.secrets = MagicMock()
        yield plugin

@pytest.fixture
def test_host():
    """Create a test host with basic variables."""
    host = MagicMock(spec=Host)
    host.name = "mysql-1"
    host.get_vars.return_value = {
        "Role": "mysql",
        "Environment": "prod",
        "Cluster": "primary",
        "node_type": "master",
        "fqdn": "mysql1.prod.example.com"
    }
    return host

class TestPathComponents:
    """Test PathComponents class functionality."""

    def test_from_tags_and_vars(self):
        """Test creating PathComponents from tags and vars."""
        tags = {"Role": "mysql", "Environment": "prod", "Cluster": "primary"}
        hostvars = {"node_type": "master", "fqdn": "mysql1.prod.example.com"}
        
        components = PathComponents.from_tags_and_vars(tags, hostvars)
        
        assert components.role == "mysql"
        assert components.environment == "prod"
        assert components.cluster == "primary"
        assert components.node_type == "master"
        assert components.fqdn == "mysql1_prod_example_com"

    def test_has_required(self):
        """Test required components checking."""
        components = PathComponents(
            role="mysql",
            environment="prod",
            cluster="primary"
        )
        
        assert components.has_required('role')
        assert components.has_required('role', 'environment')
        assert not components.has_required('role', 'node_type')

class TestVarsModule:
    """Test VarsModule functionality."""

    def test_construct_paths(self, vars_plugin, test_host):
        """Test path construction logic."""
        hostvars = test_host.get_vars()
        paths = vars_plugin._construct_paths(hostvars, hostvars)
        
        expected_paths = [
            f"{BASE_PATH}/ansible_vars",
            f"{BASE_PATH}/mysql/ansible_vars",
            f"{BASE_PATH}/mysql/global/primary/ansible_vars",
            f"{BASE_PATH}/mysql/global/primary/master/ansible_vars",
            f"{BASE_PATH}/mysql/prod/ansible_vars",
            f"{BASE_PATH}/mysql/prod/primary/ansible_vars",
            f"{BASE_PATH}/mysql/prod/primary/master/ansible_vars",
            f"{BASE_PATH}/mysql/prod/primary/mysql1_prod_example_com/ansible_vars"
        ]
        
        assert paths == expected_paths

    def test_ssm_parameter_loading(self, vars_plugin, test_host):
        """Test loading SSM parameters."""
        # Mock SSM response with mysql-specific parameters
        vars_plugin.ssm.get_paginator.return_value.paginate.return_value = [{
            'Parameters': [{
                'Name': f'{BASE_PATH}/mysql/ansible_vars',
                'Value': json.dumps({
                    'mysql_port': 3306,
                    'mysql_max_connections': 1000,
                    'mysql_innodb_buffer_pool_size': '32G'
                })
            }]
        }]
        
        hostvars = test_host.get_vars()
        values, conflicts = vars_plugin._get_ssm_parameters([f'{BASE_PATH}/mysql/ansible_vars'], hostvars)
        
        assert values == {
            'mysql_port': 3306,
            'mysql_max_connections': 1000,
            'mysql_innodb_buffer_pool_size': '32G'
        }
        assert not conflicts

    def test_variable_precedence(self, vars_plugin, test_host):
        """Test variable precedence and conflict detection."""
        # Add a conflicting host var
        hostvars = test_host.get_vars()
        hostvars['mysql_port'] = 3307
        
        # Mock SSM response with conflicting value
        vars_plugin.ssm.get_paginator.return_value.paginate.return_value = [{
            'Parameters': [{
                'Name': f'{BASE_PATH}/mysql/ansible_vars',
                'Value': json.dumps({
                    'mysql_port': 3306
                })
            }]
        }]
        
        values, conflicts = vars_plugin._get_ssm_parameters([f'{BASE_PATH}/mysql/ansible_vars'], hostvars)
        
        assert f'SSM:{BASE_PATH}/mysql/ansible_vars:mysql_port' in conflicts
        assert 'mysql_port' not in values 

    def test_skip_aws_vars(self, vars_plugin):
        """Test that hosts with skip_aws_vars=True are skipped."""
        # Create a host with skip_aws_vars set to True
        host = MagicMock(spec=Host)
        host.name = "skip-host"
        host.get_vars.return_value = {
            "skip_aws_vars": True,
            "Role": "mysql",
            "Environment": "prod",
            "Cluster": "primary"
        }

        # Test that get_vars returns empty dict
        result = vars_plugin.get_vars(None, "", host)
        assert result == {}
        
        # Verify no AWS API calls were made
        vars_plugin.ssm.get_paginator.assert_not_called()
        vars_plugin.secrets.get_paginator.assert_not_called()

    def test_skip_aws_vars_false(self, vars_plugin):
        """Test that hosts with skip_aws_vars=False still process AWS vars."""
        # Create a host with skip_aws_vars explicitly set to False
        host = MagicMock(spec=Host)
        host.name = "process-host"
        host.get_vars.return_value = {
            "skip_aws_vars": False,
            "Role": "mysql",
            "Environment": "prod",
            "Cluster": "primary"
        }

        # Mock SSM response
        vars_plugin.ssm.get_paginator.return_value.paginate.return_value = [{
            'Parameters': [{
                'Name': f'{BASE_PATH}/mysql/ansible_vars',
                'Value': json.dumps({
                    'mysql_port': 3306
                })
            }]
        }]

        # Test that get_vars processes AWS vars
        result = vars_plugin.get_vars(None, "", host)
        assert result != {}
        
        # Verify AWS API calls were made
        vars_plugin.ssm.get_paginator.assert_called_once()

    def test_skip_aws_vars_not_set(self, vars_plugin):
        """Test that hosts without skip_aws_vars set still process AWS vars."""
        # Create a host without skip_aws_vars
        host = MagicMock(spec=Host)
        host.name = "normal-host"
        host.get_vars.return_value = {
            "Role": "mysql",
            "Environment": "prod",
            "Cluster": "primary"
        }

        # Mock SSM response
        vars_plugin.ssm.get_paginator.return_value.paginate.return_value = [{
            'Parameters': [{
                'Name': f'{BASE_PATH}/mysql/ansible_vars',
                'Value': json.dumps({
                    'mysql_port': 3306
                })
            }]
        }]

        # Test that get_vars processes AWS vars
        result = vars_plugin.get_vars(None, "", host)
        assert result != {}
        
        # Verify AWS API calls were made
        vars_plugin.ssm.get_paginator.assert_called_once()