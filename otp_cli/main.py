import typer
from git import Repo
import os
from pathlib import Path
from enum import Enum
import openshift as oc
import yaml
import re
import time
import base64


########################################################################################################################
# Common printout functions
########################################################################################################################


def echo_error_msg(msg: str):
    typer.secho(msg, fg=typer.colors.RED, bold=True)


def echo_good_msg(msg: str):
    typer.secho(msg, fg=typer.colors.GREEN, bold=True)


def echo_status_msg(msg: str):
    typer.secho(msg, fg=typer.colors.CYAN, bold=True)


########################################################################################################################
# Initialisation
########################################################################################################################

app = typer.Typer()
home = os.path.expanduser("~")
otp_path = os.path.join(home, '.otp')
bootstrap_repo_path = os.path.join(otp_path, 'remote')

########################################################################################################################
# Commands
########################################################################################################################


class SetupOptions(str, Enum):
    argocd = "argocd"
    hub = "hub"


@app.callback()
def callback():
    """
    One Touch Provisioning CLI
    """


@app.command()
def setup(option: SetupOptions, roks: bool = True):
    """
    Setups ArgoCD or hub cluster
    """
    if not is_logged_in():
        echo_error_msg("Login to your OpenShift cluster first.")
        return
    else:
        echo_good_msg("Logged in.")

    if option is SetupOptions.argocd:
        echo_good_msg("Setting up ArgoCD in the hub cluster...")
        clone_upstream()

        echo_status_msg("Waiting for the ArgoCD operator to be applied...")
        argocd_operator_setup_path = os.path.join(bootstrap_repo_path, 'setup', 'ocp')
        apply_all_objects_in_directory(argocd_operator_setup_path)

        ret = run(
            "oc get subscription openshift-gitops-operator -n openshift-operators -o jsonpath='{.status.currentCSV}'")
        csv_version = ret.out.strip("'")
        while not csv_version:
            ret = run(
                "oc get subscription openshift-gitops-operator -n openshift-operators -o jsonpath='{.status.currentCSV}'")
            csv_version = ret.out.strip("'")

        echo_status_msg(f'Installing {csv_version}')

        ret = run("oc get csv " + csv_version + " -n openshift-operators -o jsonpath='{.status.reason}'")
        did_install_succeed = ret.out.strip("'") == 'InstallSucceeded'
        while not did_install_succeed:
            time.sleep(1)
            ret = run("oc get csv " + csv_version + " -n openshift-operators -o jsonpath='{.status.reason}'")
            did_install_succeed = ret.out.strip("'") == 'InstallSucceeded'

        echo_status_msg('OpenShift GitOps installed successfully.')

        echo_status_msg("Waiting for the custom ArgoCD instance to be applied...")
        argocd_instance_setup_path = os.path.join(argocd_operator_setup_path, 'argocd-instance')
        apply_all_objects_in_directory(argocd_instance_setup_path, cmd_args=['-n', 'openshift-gitops'])

        ret = run(
            "oc wait pod --timeout=-1s --for=condition=ContainersReady -l app.kubernetes.io/name=openshift-gitops-cntk-server -n openshift-gitops")
        did_install_succeed = ret.status == 0
        while not did_install_succeed:
            time.sleep(1)
            ret = run(
                "oc wait pod --timeout=-1s --for=condition=ContainersReady -l app.kubernetes.io/name=openshift-gitops-cntk-server -n openshift-gitops")
            did_install_succeed = ret.status == 0
        echo_status_msg("OpenShift GitOps instance is now available.")

        ret = run(
            "oc get route openshift-gitops-cntk-server -o template --template='https://{{.spec.host}}' -n openshift-gitops")
        url = ret.out.strip("'")

        if roks:
            echo_status_msg("Deploying on ROKS, applying default ingress TLS...")
            tls_crt_path = os.path.join(otp_path, 'tls.crt')
            tls_key_path = os.path.join(otp_path, 'tls.key')
            if os.path.exists(tls_crt_path):
                os.remove(tls_crt_path)
            if os.path.exists(tls_key_path):
                os.remove(tls_key_path)

            roks_ingress_secret = ''
            regex = r"router-default|\.(.+)\..+\.containers\.appdomain\.cloud"
            matches = re.finditer(regex, url)
            for matchNum, match in enumerate(matches, start=1):
                for groupNum in range(0, len(match.groups())):
                    roks_ingress_secret = match.group(1)

            with open(os.path.join(otp_path, 'tls.crt'), 'w+') as f:
                ret = run(
                    "oc get secret " + roks_ingress_secret + " -n openshift-ingress -o jsonpath='{.data.tls\.crt}'")
                crt = base64.b64decode(ret.out.strip("'")).decode('utf-8')
                f.writelines(crt)

            with open(os.path.join(otp_path, 'tls.key'), 'w+') as f:
                ret = run(
                    "oc get secret " + roks_ingress_secret + " -n openshift-ingress -o jsonpath='{.data.tls\.key}'")
                key = base64.b64decode(ret.out.strip("'")).decode('utf-8')
                f.writelines(key)

            ret = run(
                f"oc create -n openshift-gitops secret tls argocd-server-tls --cert={tls_crt_path} --key={tls_key_path}")
            if ret.err:
                echo_error_msg(ret.err)
            else:
                echo_status_msg("ArgoCD now using ROKS' ingress cert.")

        echo_status_msg('URL:')
        typer.echo(url)

        ret = run(
            "oc extract secrets/openshift-gitops-cntk-cluster --keys=admin.password -n openshift-gitops --to=-")
        echo_status_msg('Admin password:')
        typer.echo(ret.out.strip("'"))
    elif option is SetupOptions.hub:
        echo_error_msg("Hub cluster setup not implementing.")
    else:
        echo_error_msg("Unknown option, choose one from OPTION")


def clone_upstream():
    """
    Clones otp upstream into user's home
    """
    if not os.path.exists(bootstrap_repo_path):
        print("Upstream does not exist locally. Cloning...")
        os.makedirs(bootstrap_repo_path)
        Repo.clone_from(
            "https://github.com/one-touch-provisioning/otp-gitops",
            bootstrap_repo_path
        )
        print(f"Cloned into {bootstrap_repo_path}")


def apply_all_objects_in_directory(directory, cmd_args=None):
    objs = []

    for file in os.listdir(directory):
        filename = os.fsdecode(file)
        if filename.endswith('.yaml') or filename.endswith('.yml'):
            typer.echo(f"Applying {file}")
            filepath = os.path.join(directory, file)
            with open(filepath) as fp:
                data = list(yaml.full_load_all(fp))
                objs.extend(data)

    oc.apply(objs, cmd_args=cmd_args)


def run(command: str):
    regex = r"[\S]+"
    matches = re.finditer(regex, command)

    action = ''
    cmd_args = []
    for matchNum, match in enumerate(matches, start=1):
        if matchNum == 1:
            continue
        if matchNum == 2:
            action = match.group()
        else:
            cmd_args.append(match.group())
    cmd_args.append(None)
    ret = oc.oc_action(oc.cur_context(), action, cmd_args=cmd_args)
    return ret


def is_logged_in():
    try:
        oc.get_client_version()
        return True
    except oc.OpenShiftPythonException as err:
        return False
