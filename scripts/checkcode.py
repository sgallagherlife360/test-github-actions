import argparse
import re
import sys
import subprocess
import tempfile

from datetime import datetime, date
from github import Github

VERSION = "0.1.1"
DEFAULT_TEST_SUITE = 'phpcs'
DEFAULT_REPO_NAME = 'platform'
MUST_PASS_AFTER_DATE = '2021-06-14'
MUST_PASS_DATE_FORMAT = '%Y-%m-%d'
CLEAN_FILE_LIST = 'cleanfiles.txt'


# Known test suites
class PhpTestSuite:
    def __init__(self, name, full_name, command):
        '''

        :param name:
        :param full_name:
        :param command:
        '''
        self.name = name
        self.full_name = full_name
        self.command = command

    def command_line(self, file):
        return [file if cmd == 'FILE' else cmd for cmd in self.command]


phpcs = PhpTestSuite('phpcs', 'CodeSniffer', ['vendor/bin/phpcs', '--report-width=180', 'FILE'])
phpstan = PhpTestSuite('phpstan', 'PHPStan', ['vendor/bin/phpstan', 'analyze', '-l5', 'FILE', '--no-progress'])
phpmd = PhpTestSuite('phpmd', 'PHP Mess Detector', ['vendor/bin/phpmd', 'FILE', 'ansi', 'cleancode,codesize,controversial,design,naming,unusedcode'])

CODE_TESTS = {
    phpcs.name: phpcs,
    phpstan.name: phpstan,
    phpmd.name: phpmd
}


def parse_command_line_args():
    parser = argparse.ArgumentParser()

    # keyword/optional args
    parser.add_argument('--tool',
                        help='test suite to use: phpcs, phpstan, phpmd',
                        default=DEFAULT_TEST_SUITE)
    parser.add_argument('--file', help='override git diff and select specific file or files')
    parser.add_argument('--pr', help='number of the PR (i.e. 7664)')
    parser.add_argument('--token', help='github token to use')
    parser.add_argument('--repo', help='github repo name', default=DEFAULT_REPO_NAME)
    parser.add_argument('--update-pr', help='Attempt to add comment to the PR on GitHub', action='store_true')
    parser.add_argument('--dry-run', help='Create GitHub report without posting', action='store_true')
    parser.add_argument('--version', action='version',
                        help="Prints the version of this script and exits",
                        version='%(prog)s ' + VERSION)

    return parser


def config_validate(config):
    if config.update_pr and config.dry_run is None and (config.pr is None or config.token is None):
        print('You must provide PR number and git token')
        rc = False
    else:
        rc = True
    return rc


def get_changed_php_files(git_filter):
    git_diff = subprocess.run(['git', 'diff', 'origin/develop', '--name-only', f'--diff-filter={git_filter}'],
                              stdout=subprocess.PIPE)
    files = git_diff.stdout.strip().decode().split()
    return [file for file in files if file.endswith(".php")]


def get_date_diff_of_file(file):
    date_format_git_log = '%a, %d %b %Y %H:%M:%S %z'
    get_git_log_date = subprocess.run(['git', 'log', '--format=%aD', f'{file}'],
                                      stdout=subprocess.PIPE)
    file_added_date = get_git_log_date.stdout.strip().decode().split("\n")[-1]
    file_create_date = datetime.strptime(file_added_date, date_format_git_log).replace(tzinfo=None)
    must_pass_date = datetime.strptime(MUST_PASS_AFTER_DATE, MUST_PASS_DATE_FORMAT)
    file_age = file_create_date - must_pass_date
    return file_age.days


def generate_github_report(config, temp_file):
    test_suite = CODE_TESTS[config.tool]
    report_header(test_suite, temp_file)
    report_score, clean_list_add = run_test_suite(test_suite, temp_file)
    report_footer(config.tool, temp_file, clean_list_add)

    return report_score, clean_list_add


def run_test_suite(test_suite, temp_file):
    git_filters = {'A': 0, 'M': 0}
    clean_list_add = []
    file_count = 0
    clean_file_list = get_clean_list()

    for git_filter in git_filters.keys():
        files = get_changed_php_files(git_filter)
        file_count = file_count + len(files)

        for file in files:
            print(file)
            file_report = clean_report(test_suite.name, analyze_file(test_suite.command_line(file)))
            days_since_cutoff = get_date_diff_of_file(file)
            in_clean_list = is_clean_file(clean_file_list, file)
            file_status = 'A' if 0 < days_since_cutoff or in_clean_list else 'M'
            is_clean = create_file_report(file, file_report, file_status, temp_file)
            if not is_clean:
                git_filters[file_status] = git_filters[file_status] + 1
            elif days_since_cutoff < 0 and in_clean_list is False:
                clean_list_add.append(file)
                git_filters[file_status] = git_filters[file_status] + 1

    if file_count == 0:
        print("No updated or new PHP files")
        exit(0)

    return git_filters, clean_list_add


def run_test_suite_simple(config):
    if config.file:
        files = config.file.split(" ")
    else:
        files = get_changed_php_files('AM')

    if not files:
        print("No updated or new PHP files")
        exit(0)

    for file in files:
        print(file)
        file_report = analyze_file(CODE_TESTS[config.tool].command_line(file))
        print(file_report)


def clean_report(tool, report):
    # phpstan clean reports include a final string so len check isn't enough
    phpstan_clean_report = '[OK] No errors'
    if tool == 'phpstan' and report == phpstan_clean_report:
        return []

    # phpmd ansi output is the cleanest for github but must be stripped of coding
    if tool == 'phpmd' and len(report) > 0:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', report)

    return report


def get_file_emoji(report, git_filter):
    error_emoji = ':red_circle:'
    warning_emoji = ':yellow_circle:'
    clean_emoji = ':green_circle:'
    if len(report) and git_filter == 'A':
        return error_emoji
    elif len(report):
        return warning_emoji
    else:
        return clean_emoji


def report_header(test_suite, temp_file):
    header = """:information_source: \
    Please review and provide feedback so that we may adjust the style rules. \
    Do not mix formatting/style fixes with functional code changes in the same PR. \
    Questions or comments? Visit the #cloud-dev-chat slack channel."""

    temp_file.write(f"## {test_suite.full_name} Report :broom:\n\n")
    temp_file.write(header)


def report_footer(tool_name, temp_file, clean_list_add):
    if tool_name == 'phpcs':
        must_pass_date = datetime.strptime(MUST_PASS_AFTER_DATE, MUST_PASS_DATE_FORMAT).strftime("%B %d, %Y")
        temp_file.write(f'\\* Files created after *{must_pass_date}* OR in {CLEAN_FILE_LIST} must pass PHPCS linting.')
        if len(clean_list_add):
            clean_file_list = "\n - ".join(clean_list_add)
            temp_file.write(f'\n\n### :exclamation: These files must be added to `{CLEAN_FILE_LIST}`: \n')
            temp_file.write(f'- {clean_file_list}')


def create_file_report(file, report, git_filter, temp_file):
    is_clean = False
    emoji = get_file_emoji(report, git_filter)
    file_status = '*' if git_filter == 'A' else ''
    temp_file.write(f"<details><summary>{emoji} {file}{file_status}</summary>\n\n")
    temp_file.write("```text\n\n")

    if len(report):
        temp_file.write(report)
    else:
        is_clean = True
        temp_file.write('Cheers! This file is clean!')

    temp_file.write("\n\n```\n\n")
    temp_file.write('</details>\n\n')
    return is_clean


def analyze_file(command_line):
    report_output = subprocess.run(command_line, stdout=subprocess.PIPE)
    return report_output.stdout.strip().decode()


def make_full_repo(repo):
    return 'life360/' + repo


def update_pr_details(config, description):
    repo = Github(config.token).get_repo(make_full_repo(config.repo))
    pr = repo.get_pull(int(config.pr))
    pr.create_issue_comment(description)


def create_report_and_update_github(config):
    """create a report and post to github

    :param config: parsed configuration
    :return: boolean True iff no report messages else False
    """
    cutoff_file_code = 'A'
    with tempfile.NamedTemporaryFile(delete=False, mode="w+", newline="\n") as temp_file:
        report_results, clean_list_add = generate_github_report(config, temp_file)
        temp_file.seek(0)

        if config.dry_run:
            print(temp_file.read())
        else:
            update_pr_details(config, temp_file.read())

    return 0 == report_results[cutoff_file_code] and 0 == len(clean_list_add)


def get_clean_list():
    clean_list = open(CLEAN_FILE_LIST, "r")
    return clean_list.readlines()


def is_clean_file(clean_files, file):
    for line in clean_files:
        if file in line:
            return True
    return False


def main():
    parser = parse_command_line_args()
    config = parser.parse_args()
    if not config_validate(config):
        parser.print_help()
        sys.exit(1)

    if config.update_pr:
        rc = 0 if create_report_and_update_github(config) else 1
        if config.dry_run:
            print(f'Test {"Passed" if 0 == rc else "Failed"}')
    else:
        run_test_suite_simple(config)
        rc = 0

    sys.exit(rc)


if __name__ == "__main__":
    main()
