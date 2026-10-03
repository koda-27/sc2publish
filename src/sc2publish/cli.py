"""A small, purpose-built build and publishing CLI."""
import json
import os
import sys
from pathlib import Path

import click

from . import __version__
from .config import (
    Project,
    load_account,
    private_write,
)
from .errors import PublishError


def report(message):
    if not click.get_current_context().obj.get('quiet'):
        click.echo(message, err=True)


def output(value):
    if click.get_current_context().obj.get('json'):
        click.echo(json.dumps(value, indent=2))
    else:
        for key, item in value.items():
            label = key.replace('_', ' ').capitalize()
            click.echo(f'{click.style(f"{label:24}", bold=True)} {item}')


def project():
    instance = Project(click.get_current_context().obj['project'])
    instance.env_file = click.get_current_context().obj.get('env')
    instance.credentials_file = click.get_current_context().obj.get('credentials')
    return instance


def optional_project():
    try:
        return project()
    except PublishError:
        context = click.get_current_context()
        if context.obj['project'] or os.environ.get('SC2PUBLISH_PROJECT'):
            raise
        if any((directory / 'sc2publish.toml').exists()
               for directory in (Path.cwd(), *Path.cwd().parents)):
            raise
        return None


@click.group(context_settings={'help_option_names': ['-h', '--help']})
@click.option('-p', '--project', 'selector', help='Project TOML path or a name in your user configuration directory.')
@click.option('--env', 'env_file', type=click.Path(path_type=Path),
              help='Path to the .env file holding account credentials and settings.')
@click.option('--credentials', help='External Blizzard email/password file, or - for stdin.')
@click.option('--json', 'json_output', is_flag=True, help='Output the final result as JSON.')
@click.option('-q', '--quiet', is_flag=True, help='Suppress stage messages.')
@click.version_option(__version__)
@click.pass_context
def cli(ctx, selector, env_file, credentials, json_output, quiet):
    """Build SC2 component maps and publish directly to Battle.net.

    No editor process. No captured request templates. Private account profiles
    live outside your project, in the platform user configuration directory.
    """
    ctx.obj = {'project': selector, 'env': env_file, 'credentials': credentials,
               'json': json_output, 'quiet': quiet}


@cli.command('build')
def build_command():
    """Merge components, check sources, and build a portable MPQ archive."""
    from .build import build
    from .metadata import compile_metadata
    p = project()
    result = build(p, report)
    bundle = compile_metadata(p, 0)
    result['publication_files'] = len(bundle.files)
    result['metadata'] = 'compiled from source'
    output(result)


@cli.command()
def check():
    """Validate component sources and generated publication metadata offline."""
    from .build import check_sources, consolidate
    from .metadata import compile_metadata
    p = project()
    result = check_sources(p, consolidate(p))
    if not p.archive.exists():
        raise PublishError('Build the map first with sc2publish build.')
    bundle = compile_metadata(p, 0)
    result.update(publication_files=len(bundle.files), valid=True)
    output(result)


@cli.command()
@click.option('--remote', is_flag=True, help='Query Battle.net using a fresh authenticated connection.')
def status(remote):
    """Show saved publication state or verify the live listing."""
    p = project()
    if not remote:
        output(p.read_state() or {'name': p.name, 'status': 'No publication recorded by this project yet'})
        return
    from .connection import connect
    report('Authenticating with Battle.net')
    native = connect(p)
    try:
        result = native.prepublish()
        prior = result['m_priorRevision']
        if prior is None:
            output({'name': p.name, 'status': 'No existing listing'})
        else:
            version = prior['m_version']
            output({'name': prior['m_mapName'], 'map_id': prior['m_mapId'], 'region': p.region,
                    'version': f'{version >> 16}.{version & 65535}', 'namespace': result['m_attributeNs'],
                    'visibility': 'private' if prior['m_isPrivate'] else 'public'})
    finally:
        native.close()


@cli.command()
@click.option('--force', is_flag=True, help='Publish even if the source fingerprint has not changed.')
@click.option('--dry-run', is_flag=True, help='Build and validate the release without connecting or reserving a revision.')
def push(force, dry_run):
    """Build, upload with progress, and verify the existing listing."""
    from .publisher import publish
    instance = project()
    output(publish(instance, force=force, dry_run=dry_run, report=report))


@cli.command()
@click.option('--credentials', help='External Blizzard email/password file, or - for stdin.')
@click.option('--email', help='Blizzard email; prompted when not configured.')
@click.option('--remember', is_flag=True, help='Remember the password in the OS keyring for automatic renewal.')
@click.option('--region', type=click.Choice(['EU', 'US', 'KR']), default=None)
@click.option('--dry-run', is_flag=True, help='Check credential inputs offline; write nothing.')
def auth(credentials, email, remember, region, dry_run):
    """Sign in with your Blizzard email and password.

    Tokens and SC2 character details are obtained automatically. Passwords are
    hidden when prompted; --remember saves yours in the OS keyring. Session
    tokens are cached outside the repository. Online commands renew as needed.
    """
    from types import SimpleNamespace

    from .auth import AccountSession
    from .connection import connect

    context = click.get_current_context()
    p = optional_project()
    if p is not None and region and region != p.region:
        raise click.UsageError('--region must match the project region.')
    if p is None:
        account, _ = load_account(None, context.obj.get('env'))
        p = SimpleNamespace(base=Path.cwd(), region=region or account.get('region', 'EU').upper(), locale='enUS',
                            env_file=context.obj.get('env'), credentials_file=context.obj.get('credentials'))
    if p.region not in ('EU', 'US', 'KR'):
        raise click.UsageError('Supported regions: EU, US, KR.')
    session = AccountSession(p, credentials=credentials, email=email, remember=remember, prompt=True, force=True)
    try:
        session.read_password()
        if dry_run:
            output({'status': 'credentials valid', 'network_used': False, 'files_written': False})
            return
        report(f'Authenticating with Battle.net {p.region}')
        native = connect(p, session=session)
        try:
            output({'status': 'authenticated', 'region': p.region, 'character': native.identity['toon_name'],
                    'token_cache': str(session.env_path), 'password_storage': 'OS keyring' if remember else 'not saved'})
        finally:
            native.close()
    finally:
        session.clear_password()


cli.add_command(auth, name='login')


@cli.command()
def doctor():
    """Check local build and publishing setup without exposing credentials."""
    p = project()
    from .protocol.codec import Codec
    account, path = load_account(p, p.env_file)
    output({'version': __version__, 'python': sys.version.split()[0], 'project': str(p.path),
             'component_layers': len(p.layers), 'env_file': str(path),
             'credential_configured': bool(account.get('web_credential')),
             'blizzard_email_configured': bool(account.get('email')),
             'credentials_file_configured': bool(p.credentials_file or account.get('credentials_file')),
             'character_selection': 'automatic',
            'native_types': len(Codec().types), 'mpq_backend': 'portable Python/zlib',
             'metadata': 'compiled from source',
             'editor_required': False, 'game_installation_required': False})


@cli.command()
@click.argument('source', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--name', prompt='Map name')
@click.option('--output', 'manifest', type=click.Path(path_type=Path), default=Path('sc2publish.toml'))
def init(source, name, manifest):
    """Create a project manifest for an existing component map directory."""
    if manifest.exists():
        raise PublishError(f'{manifest} already exists.')
    text = ('[project]\n' + f'name = {json.dumps(name)}\nregion = "EU"\nlocale = "enUS"\n'
            '# Existing listing IDs are discovered by name when publishing.\n'
            '# Optionally pin map_id and namespace to assert the destination.\n'
            '\n[build]\n' + f'layers = [{json.dumps(str(source.resolve()))}]\noutput = "build"\n')
    private_write(manifest, text)
    output({'created': str(manifest), 'next': 'sc2publish build'})


def main():
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except (PublishError, OSError) as exc:
        click.echo(click.style('Error: ', fg='red', bold=True) + str(exc), err=True)
        if os.environ.get('SC2PUBLISH_DEBUG'):
            import traceback
            traceback.print_exc()
        raise SystemExit(1) from None
    except (click.Abort, KeyboardInterrupt):
        click.echo('Cancelled.', err=True)
        raise SystemExit(130) from None
