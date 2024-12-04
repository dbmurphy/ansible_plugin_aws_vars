# Ansible AWS Vars Plugin

An Ansible vars plugin that automatically injects variables from AWS Systems Manager Parameter Store (and future AWS Secrets Manager support) into your host variables based on a hierarchical path structure.

## Features

- Automatically loads variables during inventory loading
- Hierarchical variable precedence based on host attributes
- Supports JSON-formatted variables in SSM Parameter Store
- Works with both EC2 instance metadata and manual host variables
- Host vars take precedence over AWS-stored variables

## Requirements

- boto3
- requests
- ansible >= 2.9

## Installation

### Manual Installation (Recommended)

1. Create the vars plugins directory in your Ansible project if it doesn't exist:
```bash
mkdir -p /path/to/your/ansible/project/plugins/vars
```

2. Copy the plugin file to your Ansible project:
```bash
cp plugins/vars/aws_vars.py /path/to/your/ansible/project/plugins/vars/
```

### As Part of a Collection
If you want to package this as part of a collection:

```bash
# Create a collection structure
ansible-galaxy collection init your_namespace.your_collection_name

# Place the plugin in the correct location within the collection
mkdir -p your_namespace/your_collection_name/plugins/vars/
cp plugins/vars/aws_vars.py your_namespace/your_collection_name/plugins/vars/

# Build and install the collection
ansible-galaxy collection build
ansible-galaxy collection install your_namespace-your_collection_name-1.0.0.tar.gz
```

## Configuration

1. Enable the plugin in your `ansible.cfg`:
```ini
[defaults]
vars_plugins_enabled = host_group_vars,aws_vars
```

2. Configure the base path (optional):
```ini
[aws_vars]
base_path = /your/custom/path
```

3. Ensure AWS credentials are configured either via:
   - Environment variables
   - AWS credentials file
   - IAM instance profile

### Skip AWS Vars for Specific Hosts

You can skip AWS variable lookup for specific hosts by setting `skip_aws_vars: true` in your host vars. This is useful for:
- Non-AWS hosts in your inventory
- Reducing API calls during testing
- Hosts where you don't want AWS parameters

Example in inventory:
```yaml
all:
  hosts:
    aws-host-1:
      Role: mysql
      Environment: prod
      Cluster: monolith
    local-host:
      skip_aws_vars: true  # AWS vars plugin will skip this host
```

Or in host_vars file (`host_vars/local-host.yml`):
```yaml
skip_aws_vars: true
```

## Usage

### Required Host Variables

The plugin uses the following variables to construct parameter paths:

Required:
- `Role` - The role of the host (e.g., 'mysql', 'db')
- `Environment` - The environment (e.g., 'prod', 'dev')
- `Cluster` - The cluster name

Optional:
- `node_type` - Specific node type within a cluster
- `fqdn` - Fully qualified domain name of the host (dots replaced with underscores)

### Path Construction and Precedence

The plugin looks for variables in SSM Parameter Store following this path hierarchy (from lowest to highest precedence). All paths are prefixed with `BASE_PATH` (defaults to `/aws_vars` if not configured):

1. `{BASE_PATH}/ansible_vars` (Global variables)
2. `{BASE_PATH}/{Role}/ansible_vars` (Role-specific variables)
3. `{BASE_PATH}/{Role}/global/{Cluster}/ansible_vars` (Global cluster variables)
4. `{BASE_PATH}/{Role}/global/{Cluster}/{node_type}/ansible_vars` (Global cluster node type variables)
5. `{BASE_PATH}/{Role}/{Environment}/ansible_vars` (Environment variables)
6. `{BASE_PATH}/{Role}/{Environment}/{Cluster}/ansible_vars` (Cluster-specific variables)
7. `{BASE_PATH}/{Role}/{Environment}/{Cluster}/{node_type}/ansible_vars` (Node type variables)
8. `{BASE_PATH}/{Role}/{Environment}/{Cluster}/{fqdn}/ansible_vars` (Host-specific variables)

Variables from more specific paths override those from less specific paths. Host vars (defined in inventory or host_vars) always take highest precedence.

### SSM Parameter Format

Each parameter should:
- End in `/ansible_vars`
- Contain a JSON dictionary of variables

Example SSM Parameter value:
```json
{
    "database_url": "mysql://db.example.com",
    "api_key": "secret123",
    "max_connections": 100
}
```

### Using the Variables

Variables from SSM Parameter Store are loaded directly into your host variables without any prefixes. If a variable exists in both SSM Parameter Store and host_vars, the host_vars version will take precedence. They can be used like any other Ansible variable:

```yaml
name: Use loaded variables
    debug:
        msg: "Database URL is {{ database_url }}"
```

### Debugging

Run Ansible with `-v` flag to see detailed logging:
```bash
ansible-playbook -v playbook.yml
```

This will show:
- Constructed parameter paths
- Which path provided which variables
- Any conflicts with host vars
- Any errors in parameter fetching or JSON parsing

## AWS IAM Permissions

Required IAM permissions for your instance profile:
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm:GetParameter",
                "ssm:GetParametersByPath"
            ],
            "Resource": "*"
        }
    ]
}
```

Note: If running on EC2, the instance must have:
1. IMDSv2 enabled
2. An instance profile attached with the above permissions

## Variable Precedence

The final variable precedence (highest to lowest):

1. Host vars (from inventory or host_vars)
2. Host-specific vars from SSM (FQDN path)
3. Node type vars from SSM (Environment-specific)
4. Cluster-specific vars from SSM (Environment-specific)
5. Environment-specific vars from SSM
6. Global cluster node type vars from SSM
7. Global cluster vars from SSM
8. Role-specific vars from SSM
9. Global vars from SSM

## Integration with amazon.aws.aws_ec2 Inventory Plugin

This vars plugin works seamlessly with the `amazon.aws.aws_ec2` inventory plugin. While the EC2 inventory plugin discovers hosts and provides EC2 metadata, this vars plugin extends that functionality by automatically loading SSM parameters based on the EC2 instance tags.

### Setup Example

1. Install the AWS collection:
```bash
ansible-galaxy collection install amazon.aws
```

2. Configure your inventory file (`inventory.aws_ec2.yml`):
```yaml
plugin: amazon.aws.aws_ec2
regions:
  - us-east-1
keyed_groups:
  - key: tags.Role
    prefix: role
  - key: tags.Environment
    prefix: env
  - key: tags.Cluster
    prefix: cluster
filters:
  tag:Environment: prod  # Optional: filter by tags
```

3. Update your `ansible.cfg`:
```ini
[defaults]
inventory = inventory.aws_ec2.yml
vars_plugins_enabled = host_group_vars,aws_vars

[aws_vars]
base_path = /aws_vars  # Optional: customize base path
```

### How They Work Together

1. **EC2 Inventory Plugin**:
   - Discovers EC2 instances
   - Creates inventory hosts
   - Provides EC2 metadata and tags as variables
   - Groups hosts based on tags

2. **AWS Vars Plugin**:
   - Uses EC2 tags to construct SSM parameter paths
   - Loads variables from SSM Parameter Store
   - Adds these variables to the hosts

### Example

For an EC2 instance with these tags:
```yaml
Role: mysql
Environment: prod
Cluster: primary
```

The combined plugins will provide:

1. From EC2 inventory plugin:
```yaml
ansible_host: 10.0.0.1
ec2_instance_id: i-1234567890abcdef0
ec2_tag_Role: mysql
ec2_tag_Environment: prod
ec2_tag_Cluster: primary
# ... other EC2 metadata ...
```

2. From AWS vars plugin (assuming parameters exist in SSM):
```yaml
mysql_port: 3306            # from /aws_vars/mysql/prod/primary/ansible_vars
mysql_max_connections: 1000
mysql_innodb_buffer_pool_size: "32G"
# ... other parameters from SSM ...
```

### Best Practices

1. **Tag Consistency**: Ensure EC2 instances have the required tags:
   - `Role`
   - `Environment`
   - `Cluster`

2. **IAM Permissions**: Instance profile needs permissions for both plugins:
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeTags",
                "ssm:GetParameter",
                "ssm:GetParametersByPath"
            ],
            "Resource": "*"
        }
    ]
}
```

3. **Variable Precedence**: Remember that:
   - Host vars take precedence over SSM parameters
   - More specific SSM paths override less specific ones
   - EC2 tags are available as host vars

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

[MIT License](LICENSE)
