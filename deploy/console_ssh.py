"""Open/close SSH on the instance for the Lightsail *browser* console only (never 0.0.0.0/0).
Usage: python3 deploy/console_ssh.py open|close    (needs AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)"""
import sys, boto3
ls = boto3.client("lightsail", region_name="eu-west-2")
rule = {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrListAliases": ["lightsail-connect"]}
if sys.argv[1:] == ["open"]:
    ls.open_instance_public_ports(instanceName="demeisen-intranet", portInfo=rule)
elif sys.argv[1:] == ["close"]:
    ls.close_instance_public_ports(instanceName="demeisen-intranet", portInfo=rule)
else:
    sys.exit(__doc__)
for p in ls.get_instance_port_states(instanceName="demeisen-intranet")["portStates"]:
    print(p["fromPort"], p.get("cidrs"), p.get("cidrListAliases"))
