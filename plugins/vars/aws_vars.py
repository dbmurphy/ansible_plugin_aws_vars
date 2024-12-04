# pylint: disable=C0103
"""Ansible vars plugin for loading variables from AWS SSM and ASM into host variables.

This plugin automatically loads variables from AWS Systems Manager Parameter Store
and (future) AWS Secrets Manager based on host tags and attributes. It follows a
hierarchical path structure to allow for variable inheritance and overrides.

Path Structure (from generic to specific):
    /{BASE_PATH}/ansible_vars
    /{BASE_PATH}/{Role}/ansible_vars
    /{BASE_PATH}/{Role}/global/{Cluster}/ansible_vars
    /{BASE_PATH}/{Role}/global/{Cluster}/{node_type}/ansible_vars
    /{BASE_PATH}/{Role}/{Environment}/ansible_vars
    /{BASE_PATH}/{Role}/{Environment}/{Cluster}/ansible_vars
    /{BASE_PATH}/{Role}/{Environment}/{Cluster}/{node_type}/ansible_vars
    /{BASE_PATH}/{Role}/{Environment}/{Cluster}/{fqdn}/ansible_vars

Variables from more specific paths override those from less specific paths.
Host vars (from inventory or host_vars) always take highest precedence.
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

PLUGIN_DOCUMENTATION = '''
    vars: aws_vars
    short_description: Load AWS Parameters and Secrets into host vars
    description:
        - Loads parameters from AWS Systems Manager Parameter Store
        - Future support for AWS Secrets Manager
        - Uses host tags to construct hierarchical paths
        - Injects values into host_vars during inventory loading
        - Variables from more specific paths override less specific ones
        - Host vars take precedence over AWS-stored variables
    options: {}
    requirements:
        - boto3
        - requests
    notes:
        - Requires IMDSv2 enabled when running on EC2 instances
        - Instance must have appropriate IAM permissions
        - All parameters/secrets must end in /ansible_vars
        - Values must be valid JSON dictionaries
'''

import json
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass

import boto3
import requests

from ansible.plugins.vars import BaseVarsPlugin
from ansible.inventory.host import Host
from ansible.utils.display import Display

DISPLAY = Display()

# Constants
BASE_PATH = "/aws_vars"
METADATA_URL_BASE = 'http://169.254.169.254'
METADATA_TOKEN_PATH = '/latest/api/token'
METADATA_TAGS_PATH = '/latest/meta-data/tags/instance'

@dataclass
class PathComponents:
    """Components used to construct AWS secret paths.
    
    Attributes:
        role: The role of the host (e.g., 'web', 'db')
        environment: The environment name (e.g., 'prod', 'dev')
        cluster: The cluster name
        node_type: Optional node type within cluster
        fqdn: Optional fully qualified domain name (dots replaced with underscores)
    """
    role: str = ''
    environment: str = ''
    cluster: str = ''
    node_type: str = ''
    fqdn: str = ''

    @classmethod
    def from_tags_and_vars(cls, tags: Dict[str, str], hostvars: Dict[str, Any]) -> 'PathComponents':
        """Create PathComponents from tags and hostvars.
        
        Args:
            tags: Dictionary of EC2 tags or equivalent
            hostvars: Dictionary of host variables

        Returns:
            PathComponents instance with populated values
        """
        return cls(
            role=tags.get('Role', ''),
            environment=tags.get('Environment', ''),
            cluster=tags.get('Cluster', ''),
            node_type=hostvars.get('node_type', ''),
            fqdn=hostvars.get('fqdn', '').replace('.', '_')
        )

    def has_required(self, *components: str) -> bool:
        """Check if all required components are present and non-empty.
        
        Args:
            *components: Variable number of component names to check

        Returns:
            bool: True if all specified components have non-empty values
        """
        return all(bool(getattr(self, comp)) for comp in components)

# Define path patterns with descriptions
PATH_PATTERNS = {
    'BASE': [
        {'path': "{BASE_PATH}/ansible_vars", 'description': "Global variables"},
        {'path': "{BASE_PATH}/{role}/ansible_vars", 'description': "Role-specific variables"}
    ],
    'GLOBAL': [
        {'path': "{BASE_PATH}/{role}/global/{cluster}/ansible_vars", 'description': "Global cluster variables"},
        {'path': "{BASE_PATH}/{role}/global/{cluster}/{node_type}/ansible_vars", 'description': "Global cluster node type variables"}
    ],
    'ENVIRONMENT': [
        {'path': "{BASE_PATH}/{role}/{environment}/ansible_vars", 'description': "Environment variables"},
        {'path': "{BASE_PATH}/{role}/{environment}/{cluster}/ansible_vars", 'description': "Environment cluster variables"},
        {'path': "{BASE_PATH}/{role}/{environment}/{cluster}/{node_type}/ansible_vars", 'description': "Environment cluster node type variables"},
        {'path': "{BASE_PATH}/{role}/{environment}/{cluster}/{fqdn}/ansible_vars", 'description': "Host-specific variables"}
    ]
}

class VarsModule(BaseVarsPlugin):
    """Loads variables from AWS SSM and ASM for Ansible hosts.
    
    This plugin fetches variables from AWS Systems Manager Parameter Store
    and (future) AWS Secrets Manager. It constructs paths based on host
    attributes and follows a precedence order from generic to specific paths.
    """

    def __init__(self) -> None:
        """Initialize the plugin with AWS clients."""
        super().__init__()
        self.ssm = boto3.client('ssm')
        self.secrets = boto3.client('secretsmanager')
        self.token: Optional[str] = None

    def _get_imdsv2_token(self) -> Optional[str]:
        """Get IMDSv2 token for metadata access.
        
        Returns:
            str: The IMDSv2 token if successful, None otherwise
        """
        if self.token:
            return self.token

        try:
            response = requests.put(
                f"{METADATA_URL_BASE}{METADATA_TOKEN_PATH}",
                headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'},
                timeout=2
            )
            response.raise_for_status()
            self.token = response.text
            return self.token
        except requests.exceptions.RequestException as err:
            DISPLAY.v(f"Failed to get IMDSv2 token: {err}")
            return None

    def _get_instance_tags(self) -> Optional[Dict[str, str]]:
        """Get tags from instance metadata using IMDSv2.
        
        Returns:
            Dict[str, str]: Dictionary of instance tags if successful, None otherwise
        """
        token = self._get_imdsv2_token()
        if not token:
            return None

        try:
            tags_response = requests.get(
                f"{METADATA_URL_BASE}{METADATA_TAGS_PATH}",
                headers={'X-aws-ec2-metadata-token': token},
                timeout=2
            )
            tags_response.raise_for_status()
            
            tags: Dict[str, str] = {}
            for tag_key in tags_response.text.split('\n'):
                tag_value_response = requests.get(
                    f"{METADATA_URL_BASE}{METADATA_TAGS_PATH}/{tag_key}",
                    headers={'X-aws-ec2-metadata-token': token},
                    timeout=2
                )
                if tag_value_response.status_code == 200:
                    tags[tag_key] = tag_value_response.text
                    
            return tags
            
        except requests.exceptions.RequestException as err:
            DISPLAY.v(f"Error accessing instance metadata service: {err}")
            return None

    def _get_host_tags(self, host: Host) -> Dict[str, str]:
        """Get tags from host vars or instance metadata for localhost."""
        tags: Dict[str, str] = {}
        hostvars = host.get_vars()
        required_tags = ['Environment', 'Role', 'Cluster']
        missing_tags = []
        
        for tag in required_tags:
            if tag in hostvars:
                tags[tag] = hostvars[tag]
            else:
                missing_tags.append(tag)
        
        if missing_tags and (host.name == 'localhost' or host.name == '127.0.0.1'):
            instance_tags = self._get_instance_tags()
            if instance_tags:
                for tag in missing_tags[:]:
                    if tag in instance_tags:
                        tags[tag] = instance_tags[tag]
                        missing_tags.remove(tag)
        
        if missing_tags:
            DISPLAY.v(
                f"Host {host.name} is missing required tags for AWS secret lookup: "
                f"{', '.join(missing_tags)}. Cannot construct ASM/SSM path."
            )
            DISPLAY.v(f"Current tags found: {tags}")
            
        return tags

    def _add_path_if_valid(self, paths: List[str], path: str) -> None:
        """Add path to list if all components are valid.
        
        Args:
            paths: List of paths to append to
            path: Path to validate and potentially add
        """
        if all(bool(component) for component in path.split('/') if '{' not in component):
            paths.append(path)

    def _construct_paths(self, tags: Dict[str, str], hostvars: Dict[str, Any]) -> List[str]:
        """Construct all possible secret/parameter paths in order of precedence."""
        paths: List[str] = []
        components = PathComponents.from_tags_and_vars(tags, hostvars)

        # Process each path group in order
        for group_name, patterns in PATH_PATTERNS.items():
            for pattern in patterns:
                try:
                    path = pattern['path'].format(
                        BASE_PATH=BASE_PATH,
                        role=components.role,
                        environment=components.environment,
                        cluster=components.cluster,
                        node_type=components.node_type,
                        fqdn=components.fqdn
                    )
                    self._add_path_if_valid(paths, path)
                    DISPLAY.v(f"Added {group_name} path: {path} ({pattern['description']})")
                except KeyError as e:
                    # Extract the missing component name from the KeyError message
                    missing_component = str(e).strip("'")
                    DISPLAY.v(
                        f"Skipping pattern '{pattern['path']}' ({pattern['description']}) "
                        f"because required component '{missing_component}' is missing or empty. "
                        f"Current components: {components}"
                    )
                    continue

        DISPLAY.v(f"Constructed paths in order of precedence (generic to specific): {paths}")
        return paths

    def _get_ssm_parameters(self, paths: List[str], hostvars: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """Get parameters from SSM following path precedence."""
        values: Dict[str, Any] = {}
        conflicts: List[str] = []
        
        for path in paths:
            try:
                paginator = self.ssm.get_paginator('get_parameters_by_path')
                for page in paginator.paginate(Path=path, Recursive=False, WithDecryption=True):
                    for param in page['Parameters']:
                        if not param['Name'].endswith('/ansible_vars'):
                            continue
                        try:
                            param_values = json.loads(param['Value'])
                            if not isinstance(param_values, dict):
                                DISPLAY.v(f"SSM parameter '{param['Name']}' is not a JSON dictionary, skipping")
                                continue
                                
                            for var_name, var_value in param_values.items():
                                if var_name in hostvars:
                                    conflicts.append(f"SSM:{path}:{var_name}")
                                    DISPLAY.v(f"Parameter '{var_name}' from path '{path}' exists in host_vars, using host_vars value")
                                    continue
                                values[var_name] = var_value
                                DISPLAY.v(f"Setting/updating '{var_name}' from SSM path: {path}")
                        except json.JSONDecodeError:
                            DISPLAY.v(f"SSM parameter '{param['Name']}' is not valid JSON, skipping")
                            continue
            except Exception as err:
                DISPLAY.v(f"Error getting SSM parameters for path {path}: {err}")
                continue

        return values, conflicts

    # pylint: disable=unused-private-member
    def _get_asm_secrets(self, paths: List[str], hostvars: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """Get secrets from ASM following path precedence."""
        values: Dict[str, Any] = {}
        conflicts: List[str] = []
        
        for path in paths:
            paginator = self.secrets.get_paginator('list_secrets')
            for page in paginator.paginate(Filters=[{'Key': 'path', 'Values': [path]}]):
                for secret in page['SecretList']:
                    if not secret['Name'].endswith('/ansible_vars'):
                        continue
                    try:
                        value = self.secrets.get_secret_value(SecretId=secret['ARN'])
                        try:
                            secret_values = json.loads(value['SecretString'])
                            if not isinstance(secret_values, dict):
                                DISPLAY.v(f"Secret '{secret['Name']}' is not a JSON dictionary, skipping")
                                continue
                                
                            for var_name, var_value in secret_values.items():
                                if var_name in hostvars:
                                    conflicts.append(f"ASM:{path}:{var_name}")
                                    DISPLAY.v(f"Secret '{var_name}' from path '{path}' exists in host_vars, using host_vars value")
                                    continue
                                values[var_name] = var_value
                                DISPLAY.v(f"Setting/updating '{var_name}' from ASM path: {path}")
                        except json.JSONDecodeError:
                            DISPLAY.v(f"Secret '{secret['Name']}' is not valid JSON, skipping")
                            continue
                    except Exception as err:
                        DISPLAY.v(f"Error getting secret {secret['Name']}: {err}")
                        continue
        
        return values, conflicts

    # pylint: disable=unused-private-member
    def _get_asm_secret(self, path: str, secret_name: str, hostvars: Dict[str, Any]) -> Tuple[Optional[str], bool]:
        """Get a single secret from ASM."""
        secret_id = f"{path}/{secret_name}"
        try:
            response = self.secrets.get_secret_value(SecretId=secret_id)
            return response['SecretString'], secret_name in hostvars
        except self.secrets.exceptions.ResourceNotFoundException:
            DISPLAY.v(f"Secret not found: {secret_id}")
        except Exception as err:
            DISPLAY.v(f"Error retrieving secret {secret_id}: {err}")
        
        return None, False

    # pylint: disable=unused-argument
    def get_vars(self, loader: Any, path: str, entities: Any, cache: bool = True) -> Dict[str, Any]:
        """Main entry point for vars plugin."""
        if not isinstance(entities, Host):
            return {}

        host = entities
        hostvars = host.get_vars()

        # Check for skip flag
        if hostvars.get('skip_aws_vars', False):
            DISPLAY.v(f"Skipping AWS vars lookup for host {host.name} due to skip_aws_vars=True")
            return {}

        ret: Dict[str, Any] = {}
        
        tags = self._get_host_tags(host)
        paths = self._construct_paths(tags, hostvars)
        if not paths:
            DISPLAY.v(f"No valid paths could be constructed for host {host.name}")
            return ret

        ssm_values, ssm_conflicts = self._get_ssm_parameters(paths, hostvars)
        ret.update(ssm_values)
        
        # Future ASM implementation
        # asm_values, asm_conflicts = self._get_asm_secrets(paths, hostvars)
        # ret.update(asm_values)
        # conflicts.extend(asm_conflicts)
        
        if ssm_conflicts:  # Future: Combine with asm_conflicts
            DISPLAY.warning(
                f"Host {host.name} has secrets in host_vars that override AWS secrets: "
                f"{', '.join(ssm_conflicts)}"
            )

        return ret