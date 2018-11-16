#!/usr/bin/env python

import argparse
from collections import OrderedDict
import concurrent.futures as cf
from itertools import zip_longest
import os
import subprocess
import sys

import colorama
from colorama import (
    Back,
    Fore,
    Style,
)
from git import (
    BadName,
    GitCommandError,
    InvalidGitRepositoryError,
    Repo,
)
import sh


# Env var to look in for path to folder containing repos
REPOS_PATH_ENV_VAR = 'GIT_SUMMARY_REPOS_PATH'


EPILOG = """
README
======

This tool writes a status summary table for git repos in some folder.  Within
that folder, every folder containing a git repo is summarised in a single row
of the table.

The target folder may be specified on the command line or read from an
environment variable ({repos_path_env_var}).

The table includes the following information for each repo:

* The repo name (just the name of the folder it's in)

* The active branch name

* A short string summarising various aspects of the repo; the characters mean:

    ?  untracked files
    +  new (staged) files
    m  unstaged modifications to files
    M  staged modifications to files
    R  renamed files
    v  unpulled commits
    ^  unpushed commits

    00000    no commits in repo yet
         --  no remote tracking branch
         @@  tracking branch is gone on remote
         XX  error fetching from remote

* Optionally (--tracking/-t flag), remote/tracking branch names

Ordinarily, the tool doesn't hit the network at all; however, it can perform a
"git fetch" on every repo in order to properly check for unpulled/unpushed
commits.  As this can be a slow operation, it is disabled by default, but can
be activated using the --fetch/-f flag.

Output is coloured by default, using colours suited to a black background.
Disable colours via the --monochrome/-m flag.

The meanings of the colours are:

    Green            Everything good
    Red              Local has uncommitted changes
    Yellow           Local good but branch has no remote (or not fetched yet)
    Cyan             Local good but unpulled/unpushed commits
    Magenta          Local good but tried and failed to "git fetch"
    Inverted yellow  Repo has no commits yet

By default the tool writes 'fancy' output, in a table whose cells are filled in
as soon as the information becomes available.  As this uses ANSI escape codes
which can cause problems in some settings, it can be disabled via the
--simple/-s flag (which also implies --monochrome).  This happens automatically
if the tool's output is being redirected or piped anywhere.

The 'fancy' output mode tries to write its output in the right place in the
console, so as not to disrupt your terminal's scrollback history; if it has
trouble with that, it will clear the screen first and start writing at the top.
You can force it to clear the screen via the --clear/-c flag (which has no
effect in --simple output mode).

By default the tool runs concurrently, querying multiple repos in the
background at once.  If desired, this can be disabled via the --sequential/-S
flag.  It's somewhat slower, particular if also fetching from remotes.

""".format(repos_path_env_var=REPOS_PATH_ENV_VAR)


HEADER_REPO = 'repo name'
HEADER_BRANCH = 'branch'
HEADER_STATE = 'state'
HEADER_TRACKING = 'tracking branch'
HEADER_LINE = '='

COLORS = {
    'all good': [Fore.GREEN],
    'local dirty': [Fore.RED],
    'local good; no remote': [Fore.YELLOW],
    'remote dirty': [Fore.CYAN],
    'fetch failed': [Fore.MAGENTA],
    'no commits yet': [Fore.BLACK, Back.YELLOW],
}


def main():

    args = parse_args()
    if not sys.stdout.isatty():
        # If output is being redirected somewhere, turn off fancy output.
        args.simple = True

    # Repos are folders which have .git folders inside them
    candidates = [f for f in os.listdir(args.path)
                  if os.path.isdir(os.path.join(args.path, f, '.git'))]
    # But we filter out any that aren't *really* git repos...
    repos = [repo for repo in
             [GitRepo.construct(args.path, c) for c in sorted(candidates)]
             if repo]

    if not repos:
        print('No git repos found at path: {}'.format(args.path))
        sys.exit(1)

    if args.simple:
        output_class = SimpleOutput
    else:
        output_class = FancyOutput
        colorama.init()

    if args.sequential:
        summariser_class = SequentialSummariser
    else:
        summariser_class = ConcurrentSummariser

    output = output_class(repos, **vars(args))
    output.initial()
    summariser = summariser_class(repos, output, args.fetch)
    summariser.run()


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG)
    repos_path = os.environ.get(REPOS_PATH_ENV_VAR, None)
    parser.add_argument(
        'path', nargs='?', metavar='PATH', default=repos_path,
        help=('Path to folder containing repos; default is from '
              '{} env var ("{}")').format(REPOS_PATH_ENV_VAR, repos_path))
    parser.add_argument(
        '-t', '--tracking', action='store_true',
        help='display tracking branch nane')
    parser.add_argument(
        '-f', '--fetch', action='store_true',
        help=("run a 'git fetch' on each repo before reporting its "
              'remote status (this can be slow)'))
    parser.add_argument(
        '-m', '--monochrome', action='store_true',
        help="don't use colors in output")
    parser.add_argument(
        '-s', '--simple', action='store_true',
        help=('use simple output, i.e. write results sequentially '
              'not concurrently. Always true if output is redirected '
              'or piped. Implies -m'))
    parser.add_argument(
        '-c', '--clear', action='store_true',
        help='always clear the screen in fancy output mode')
    parser.add_argument(
        '-S', '--sequential', action='store_true',
        help=('check repo states sequentially, not concurrently '
              '(slower but helpful in case of weirdness)'))

    args = parser.parse_args()
    if not args.path:
        print('No path specified, and none in {} env var'.format(
            REPOS_PATH_ENV_VAR))
        print('Run "{} -h" for more information'.format(
            os.path.basename(sys.argv[0])))
        sys.exit(1)
    args.path = os.path.abspath(os.path.expanduser(args.path))

    return args


class GitRepo:

    @classmethod
    def construct(cls, path, name):
        """Factory method: if not a valid git repo, return None."""
        try:
            return cls(path, name)
        except InvalidGitRepositoryError:
            return None

    def __init__(self, path, name):
        """GitRepo constructor.

        path - path to folder containing repo (i.e. the parent folder).

        name - name of repo (i.e. the name of the folder within path
               which contains the repo.

        """
        self.path = path
        self.name = name
        self.repo = Repo(os.path.join(self.path, self.name))
        try:
            self.branch_name = self.repo.active_branch.name
        except TypeError:
            # Probably a detached head
            self.branch_name = '--- detached? ---'

        # A git repo can be initialised but have no commits, in which case
        # there's not much to report about it.  Is that the case here?
        self.has_commits = None

        # Local state flags; until self.has_commits becomes True, these will
        # all remain None.
        self.has_untracked_files = None
        self.has_new_files = None
        self.has_unstaged_modifications = None
        self.has_staged_modifications = None
        self.has_renamed_files = None

        # Does this repo's active branch have a remote tracking branch set up?
        self.has_remote = None

        # Remote state info; until self.has_remote becomes True, these will all
        # remain None.
        self.remote_branch = None
        self.remote_name = None
        self.remote_is_gone = None
        # This will become True if we try to do a git fetch and there's a
        # problem.
        self.fetch_failed = None
        # Until self.fetch_failed becomes False, these will remain None.
        self.has_unpulled_commits = None
        self.has_unpushed_commits = None

    @property
    def local_dirty(self):
        """Is this repo's local git state dirty in some way?"""
        return (self.has_commits and
                any((self.has_untracked_files,
                     self.has_new_files,
                     self.has_unstaged_modifications,
                     self.has_staged_modifications,
                     self.has_renamed_files)))

    @property
    def remote_dirty(self):
        """Is this repo's remote git state dirty in some way?"""
        return (self.has_remote and
                any((self.has_unpulled_commits,
                     self.has_unpushed_commits)))

    @property
    def tracking_branch(self):
        """If repos' activate branch has a remote tracking branch, return it.

        Returns a string: either remote_name/branch_name or empty string.

        """
        if self.remote_name and self.remote_branch:
            return '{}/{}'.format(self.remote_name, self.remote_branch)
        else:
            return ''

    def get_local_state(self):
        """Get this repo's local state - new/modified files, etc."""
        try:
            diff = self.repo.index.diff
            unstaged_diffs = [d.change_type for d in diff(None)]
            staged_diffs = [d.change_type for d in diff('HEAD')]
        except BadName:
            # Git repo has been initialised but has no commits yet.
            self.has_commits = False
            return
        self.has_commits = True
        self.has_untracked_files = bool(self.repo.untracked_files)
        self.has_new_files = 'D' in staged_diffs
        self.has_unstaged_modifications = 'M' in unstaged_diffs
        self.has_staged_modifications = 'M' in staged_diffs
        self.has_renamed_files = 'R100' in staged_diffs

    def get_remote_state(self, fetch):
        """Get this repo's remote state - unpulled/unpushed commits."""
        try:
            local_branch = self.repo.active_branch
        except TypeError:
            # Probably detached
            self.has_remote = False
            return
        remote_branch = local_branch.tracking_branch()
        if not remote_branch:
            self.has_remote = False
            return
        self.has_remote = True
        self.remote_branch = remote_branch.remote_head
        self.remote_name = remote_branch.remote_name
        if fetch:
            try:
                self.repo.remotes[self.remote_name].fetch()
            except GitCommandError:
                self.fetch_failed = True
                return
        self.fetch_failed = False
        self.remote_is_gone = self.check_if_remote_is_gone()
        if self.remote_is_gone:
            return
        self.has_unpulled_commits = self.git_log_cmp(
            local_branch, remote_branch)
        self.has_unpushed_commits = self.git_log_cmp(
            remote_branch, local_branch)

    def check_if_remote_is_gone(self):
        os.chdir(os.path.join(self.path, self.name))
        try:
            # If this command fails, the remote is gone.
            sh.git((
                'show-branch',
                '{}/{}'.format(self.remote_name, self.remote_branch),
            ))
            return False
        except sh.ErrorReturnCode:
            return True

    def git_log_cmp(self, branch_1, branch_2):
        """Does branch_1 contain commits which aren't in branch_2?

        Here we call out to an external "git log" process - I couldn't
        work out a way to compute this result using gitpython.

        """
        os.chdir(os.path.join(self.path, self.name))
        args = ('--no-pager', 'log', '--format=oneline',
                '{}..{}'.format(branch_1.name, branch_2.name))
        return bool(sh.git(args).stdout.decode('utf-8').strip())


class SequentialSummariser:

    # It doesn't get much simpler than this.

    def __init__(self, repos, output, fetch):
        self.repos = repos
        self.output = output
        self.fetch = fetch

    def run(self):
        """Summarise repos sequentially."""
        for repo in self.repos:
            repo.get_local_state()
            self.output.got_local_state(repo)
            repo.get_remote_state(self.fetch)
            self.output.got_remote_state(repo)


class ConcurrentSummariser:

    # Faster, but more complicated.

    def __init__(self, repos, output, fetch):
        self.repos = repos
        self.output = output
        self.fetch = fetch

    def run(self):
        """Summarise repos concurrently using concurrent.futures."""
        with cf.ProcessPoolExecutor(8) as executor:
            self.executor = executor
            local_futures = self.launch_local_state_checks()
            self.launch_remote_state_checks(local_futures)

    def launch_local_state_checks(self):
        """Trigger local state checks for all repos.."""
        local_futures = []
        for repo in self.repos:
            # Set up a call to repo.get_local_state in another process; when
            # it's done, tell the output to update repo's local state.
            future = self.submit(self.executor, repo.get_local_state)
            self.add_callback(future, self.output.got_local_state)
            local_futures.append(future)
        return local_futures

    def launch_remote_state_checks(self, local_futures):
        """Trigger remote state checks as local state checks complete."""
        remote_futures = []
        for completed in cf.as_completed(local_futures):
            repo = completed.result()
            # Set up a call to repo.get_remote_state in another process;
            # when it's done, tell the output to update repo's remote state.
            future = self.submit(
                self.executor, repo.get_remote_state, self.fetch)
            self.add_callback(future, self.output.got_remote_state)
            remote_futures.append(future)

        # Wait for everything to finish.
        cf.wait(remote_futures)

    @classmethod
    def submit(cls, executor, bound_method, *args, **kwargs):
        """Submit a bound method for concurrent execution.

        Given an executor, a bound method, and maybe some arguments,
        tell the executor to schedule a concurrent call of that bound
        method with those arguments, and to return, on completion, the
        object which the method is bound to.

        (This allows us to call a method on a GitRepo object in a
        future, and have the future return the (modified) GitRepo object
        when it's finished - even if the GitRepo method doesn't
        explicitly return self.

        """
        return executor.submit(
            cls.wrapped_repo_call, bound_method, *args, **kwargs)

    @staticmethod
    def wrapped_repo_call(bound_method, *args, **kwargs):
        """Wrapper for repo methods which always returns repo."""
        bound_method(*args, **kwargs)
        return bound_method.__self__

    @classmethod
    def add_callback(cls, future, callback):
        """Add a callback to be triggered upon future's completion.

        Given a future and a callable which expects one parameter, this
        adds a callback to the future which, when the future completes,
        extracts the future's result and calls the callable, passing
        that result as the single parameter.

        (In our case that "one parameter" will be a GitRepo object,
        i.e. the one just updated by whatever the future was doing.)

        """
        future.add_done_callback(lambda future: callback(future.result()))


class OutputBase:

    """Base class for outputs, containing shared behaviour."""

    def __init__(self, repos, path, tracking=False, *args, **kwargs):
        self.repos = OrderedDict((repo.name, repo) for repo in repos)
        self.path = path
        self.tracking = tracking

    @property
    def max_repo_len(self):
        """Length of longest repo name."""
        return max(
            [len(HEADER_REPO)] +
            [len(repo_name) for repo_name in self.repos]
        )

    @property
    def max_branch_len(self):
        """Length of longest branch name."""
        return max(
            [len(HEADER_BRANCH)] +
            [len(repo.branch_name) for repo in self.repos.values()]
        )

    def local_state_string(self, repo):
        """Compute compact string representing local state."""
        if not repo.has_commits:
            return '00000'
        facts = (
            ('?', repo.has_untracked_files),
            ('+', repo.has_new_files),
            ('m', repo.has_unstaged_modifications),
            ('M', repo.has_staged_modifications),
            ('R', repo.has_renamed_files),
        )
        return self.condense_facts(facts)

    def remote_state_string(self, repo):
        """Compute compact string representing state vs remote."""
        if not repo.has_remote:
            return '--'
        if repo.remote_is_gone:
            return '@@'
        if repo.fetch_failed:
            return 'XX'
        facts = (
            ('v', repo.has_unpulled_commits),
            ('^', repo.has_unpushed_commits),
        )
        return self.condense_facts(facts)

    def condense_facts(self, facts, default=' '):
        """Turn a list of (<char>, <bool>) pairs into a compact string.

        For each (<char>, <bool>) pair, the corresponding character in
        the returned string will be <char> if <bool> is True, or
        <default> otherwise.

        """
        return ''.join((c if cond else default for c, cond in facts))


class SimpleOutput(OutputBase):

    def __init__(self, repos, path, tracking=False, *args, **kwargs):
        super().__init__(repos, path, tracking, *args, **kwargs)
        self.repos_locals = {repo.name: False for repo in repos}
        self.repos_remotes = {repo.name: False for repo in repos}
        # Dictionary mapping repo name to name of next repo in list
        repo_names = [repo.name for repo in repos]
        self.next_repos = dict(zip_longest(repo_names, repo_names[1:]))

    def initial(self):
        """Start writing simple output: header and start of first row."""
        self.write_header()
        first_repo = list(self.repos.values())[0]
        self.print_repo_name(first_repo.name)
        self.print_branch_name(first_repo.branch_name)
        # At this point we've written the first repo's name and branch, and
        # we're waiting on its local state.  All further output is triggered by
        # calls to the got_local_state/got_remote_state callbacks.
        self.position = first_repo.name, True

    def write_header(self):
        """Write header rows of result table."""
        print('git summary for {}'.format(self.path))
        print()
        self.print_repo_name(HEADER_REPO)
        self.print_branch_name(HEADER_BRANCH)
        self.print_local_state(HEADER_STATE)
        self.print_remote_state('  ', HEADER_TRACKING if self.tracking else None)
        self.print_repo_name(HEADER_LINE * self.max_repo_len)
        self.print_branch_name(HEADER_LINE * self.max_branch_len)
        self.print_local_state(HEADER_LINE * 7)
        self.print_remote_state('', '=' * len(HEADER_TRACKING))

    def print_repo_name(self, repo_name):
        """Print appropriately-padded repo name."""
        print(('{:<%d}  ' % self.max_repo_len).format(repo_name), end='')

    def print_branch_name(self, branch_name):
        """Print appropriately-padded branch name."""
        print(('{:<%d}  ' % self.max_branch_len).format(branch_name), end='')

    def print_local_state(self, local_state):
        """Print appropriately-padded local state info."""
        print(local_state, end='')

    def print_remote_state(self, remote_state, tracking_branch):
        """Print appropriately-padded remote state info."""
        print(remote_state, end='')
        if self.tracking:
            print('  {}'.format(tracking_branch).rstrip())
        else:
            print()

    def got_local_state(self, repo):
        """Callback triggered when repo's local state has been computed."""
        self.repos[repo.name] = repo
        self.repos_locals[repo.name] = True
        self.write_outstanding_info()

    def got_remote_state(self, repo):
        """Callback triggered when repo's remote state has been computed."""
        self.repos[repo.name] = repo
        self.repos_remotes[repo.name] = True
        self.write_outstanding_info()

    def write_outstanding_info(self):
        """Write any info waiting to be written.

        This is triggered when we've got some new information.  We write
        forwards from where we are, as far as possible, until we run out
        of information to write.  It's possible there's *nothing* more
        to write at this time - i.e. if the new information is not the
        next thing we're currently waiting to write.  On the other hand,
        we may be able to move on multiple steps, or even all the way to
        the end.

        """
        while True:
            moved = self.maybe_write_more_info()
            if not moved:  # No change, so we're done for now.
                break

    def maybe_write_more_info(self):
        """One step of the write_outstanding_info() cycle.

        Given our current position in the output stream, is the
        information we want to write next available now?  If so, write
        it and move on to the next position; otherwise do nothing.

        Returns a boolean indicating whether or not we moved on or not.

        """
        # Current position is a repo name and a flag indicating if we're
        # waiting for its local state info (True) or its remote state info
        # (False).
        repo_name, waiting_on_local = self.position
        if not repo_name:
            return False  # Finished; nothing to do (shouldn't ever happen)
        repo = self.repos[repo_name]
        if waiting_on_local:
            # Waiting for this repo's local state: did it arrive?
            if not self.repos_locals[repo_name]:
                return False  # Still don't have it; nothing to do.
            self.print_local_state(self.local_state_string(repo))
            # Now waiting on this repo's remote state
            self.position = repo_name, False
            return True
        else:
            # Waiting for this repo's remote state: did it arrive?
            if not self.repos_remotes[repo_name]:
                return False  # Still don't have it; nothing to do.
            self.print_remote_state(
                self.remote_state_string(repo), repo.tracking_branch)
            # We're done with this repo: move on the next (if any)
            next_repo_name = self.next_repos[repo_name]
            if next_repo_name is None:
                return False  # No more repos, we've finished, woo.
            self.print_repo_name(next_repo_name)
            self.print_branch_name(self.repos[next_repo_name].branch_name)
            # Now ready and waiting to write local state of this next repo.
            self.position = next_repo_name, True
            return True


class FancyOutput(OutputBase):

    """Fancy fast-updating output using ANSI escape codes."""

    def __init__(self, repos, path, tracking=False,
                 monochrome=False, clear=False, *args, **kwargs):
        super().__init__(repos, path, tracking, *args, **kwargs)
        self.monochrome = monochrome
        self.clear = clear
        self.rows = {repo.name: i for i, repo in enumerate(repos)}
        self.row0 = 2  # 1st row of table header: column names
        self.row1 = self.row0 + 1  # 2nd row of table header: =====

    def initial(self):
        """Start writing fancy output: header, repo/branch names."""
        # Set up helper to make space on the screen and write into it.
        self.ansi = AnsiWriter(len(self.repos) + self.row1 + 1, self.clear)
        # Write column headings and repo names, branches, and "state unknown"
        self.write_header()
        for repo in self.repos.values():
            self.repo_write(repo, 0, repo.name)
            self.repo_write(repo, self.x_b, repo.branch_name)
            self.repo_write(repo, self.x_s, '_______')

    def repo_write(self, repo, col, m):
        """Write some msg in some column for some repo."""
        self.ansi.write_at(self.rows[repo.name] + self.row1 + 1, col, m)

    @property
    def max_tracking_len(self):
        """Length of longest tracking branch name."""
        return max([len(HEADER_TRACKING)] +
                   [len(repo.tracking_branch) for repo in self.repos.values()])

    @property
    def x_b(self):
        """Starting column for branch information."""
        return self.max_repo_len + 3

    @property
    def x_s(self):
        """Starting column for state information."""
        return self.x_b + self.max_branch_len + 2

    @property
    def x_t(self):
        """Starting column for tracking branch information."""
        return self.x_s + 7 + 2

    def write_header(self):
        """Write header rows of result table."""
        write = self.ansi.write_at
        write(0, 0, 'git summary for {}'.format(self.path))
        write(self.row0, 0, HEADER_REPO)
        write(self.row1, 0, HEADER_LINE * self.max_repo_len)
        write(self.row0, self.x_b, HEADER_BRANCH)
        write(self.row1, self.x_b, HEADER_LINE * self.max_branch_len)
        state_header = HEADER_STATE + '  '  # Make ==== line longer
        write(self.row0, self.x_s, state_header)
        write(self.row1, self.x_s, HEADER_LINE * len(state_header))
        if self.tracking:
            write(self.row0, self.x_t, HEADER_TRACKING)
            write(self.row1, self.x_t, HEADER_LINE * self.max_tracking_len)

    def write_repo_name(self, repo):
        """Write an repo's name, maybe in an informative/garish colour."""
        color = self.repo_color(repo)
        self.repo_write(repo, 0, self.colorise(repo.name, *color))

    def repo_color(self, repo):
        """Compute color for repo."""
        if not repo.has_commits:
            return COLORS['no commits yet']
        elif repo.local_dirty:
            return COLORS['local dirty']
        elif not repo.has_remote:
            return COLORS['local good; no remote']
        elif repo.fetch_failed:
            return COLORS['fetch failed']
        elif repo.remote_dirty:
            return COLORS['remote dirty']
        else:
            return COLORS['all good']

    def got_local_state(self, repo):
        """Callback triggered when repo's local state has been computed."""
        self.repos[repo.name] = repo  # Update local copy of repo
        self.write_repo_name(repo)  # Enact any necessary colour change
        self.repo_write(repo, self.x_s, self.local_state_string(repo))

    def got_remote_state(self, repo):
        """Callback triggered when repo's remote state has been computed."""
        self.repos[repo.name] = repo  # Update local copy of repo
        self.write_repo_name(repo)  # Enact any necessary colour change
        self.repo_write(repo, self.x_s + 5, self.remote_state_string(repo))
        if self.tracking:
            self.repo_write(repo, self.x_t, repo.tracking_branch)
            self.write_header()  # Maybe extend === for tracking branch

    def colorise(self, s, *styles):
        """Potentially wrap a string in colorising info."""
        if self.monochrome:
            return s
        return '{}{}{}'.format(''.join(styles), s, Style.RESET_ALL)


class AnsiWriter:

    """Helper class for moving the cursor with ANSI escape codes.

    Easily write to arbitrary (0-indexed) positions in the console.

    rows_needed - How many rows are we going to need to be able to write into?

    force_clear - Clear the screen before writing anything?  (Will do that
                  anyway if can't compute current cursor position.)

    """

    def __init__(self, rows_needed, force_clear=False):
        self.rows_needed = rows_needed
        # We want to write our output within the existing flow of the console,
        # and not disrupt scrollback/history; that relies on being able to work
        # out the cursor's current position in the console, so we can position
        # and write relative to that.  If we can't do that, the fallback is to
        # clear the screen and start at the top.
        if force_clear or not self._get_current_row():
            # Either user asked explicitly to clear the screen, or we can't
            # work out where we are on the screen; either way, we'll just start
            # writing output at the top (which will force the screen to clear).
            self.root_row = 0
        else:
            # Looks like we *can* work out where we are, so make (vertical)
            # space for all that we need to write, and start writing from the
            # top of that space.
            print('\n' * rows_needed)
            self.root_row = self._get_current_row() - rows_needed - 1

        # Starting at the top?  Clear the screen!
        if self.root_row == 0:
            self.clear()

    def clear(self):
        """Clear the console, then reset the cursor."""
        print('\x1b[2J', end='')
        self._reset()
        sys.stdout.flush()

    def write_at(self, row, col, msg):
        """Write given message at given position, then reset the cursor."""
        print(self._pos(row + 1, col) + msg, end='')
        self._reset()
        sys.stdout.flush()

    def _pos(self, row, col):
        """Move the cursor to the given position.."""
        return '\x1b[%d;%dH' % (self.root_row + row, col)

    def _reset(self):
        """Reset the cursor: move to column 0 in row after final row."""
        print(self._pos(self.rows_needed, 0))

    @classmethod
    def _get_current_row(cls):
        """Try to learn cursor's row in terminal; return int or None (error)."""
        # https://unix.stackexchange.com/a/183121/181714
        # via http://stackoverflow.com/a/2575525
        script = "IFS=';' read -sdR -p $'\E[6n' ROW COL;echo \"${ROW#*[}\""
        try:
            p = subprocess.Popen(script, shell=True, stdout=subprocess.PIPE)
            return int(p.communicate(timeout=1)[0].decode('utf-8').strip()) - 1
        except:
            return None


if __name__ == '__main__':
    main()
