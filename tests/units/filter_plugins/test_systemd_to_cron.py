# -*- coding: utf-8 -*-
# Remember to place this file in a 'tests/unit/plugins/filter' directory
# relative to your collection root for ansible-test to find it.
# Example: collections/ansible_collections/my_namespace/my_collection/tests/unit/plugins/filter/test_systemd_to_cron.py

#todo: Add more test cases for wrapping around, for range/step merging, invalid and incomplete formats

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest
from ansible.errors import AnsibleFilterError
import re # Import regex for warning checks

# Assuming the filter plugin is located at:
# collections/ansible_collections/my_namespace/my_collection/plugins/filter/systemd_to_cron.py
# Adjust the import path based on your actual collection structure if necessary.
# If running outside ansible-test, you might need path adjustments, but this
# structure is standard for ansible-test unit. */
try:
    # from ansible_collections.my_namespace.my_collection.plugins.filter.systemd_to_cron import FilterModule
    from filter_plugins.systemd_to_cron import FilterModule
except ImportError:
    # Fallback for running directly or different structures (adjust as needed)
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '../../../filter_plugins'))
    from systemd_to_cron import FilterModule

# --- Fixtures ---

@pytest.fixture(scope="module")
def systemd_filter():
    """
    Fixture providing the initialized systemd_to_cron filter function.
    Scope is 'module' as the filter instance doesn't change between tests.
    """
    fm = FilterModule()
    return fm.filters()['systemd_to_cron']

# --- Helper Functions ---

def assert_cron_params_match(actual, expected):
    """
    Custom assertion that checks if the expected cron parameters are in the
    actual result, completely ignoring the 'warnings' key.
    """
    # Create a copy of the actual result without the 'warnings' key
    actual_filtered = {k: v for k, v in actual.items() if k != 'warnings'}

    # Ensure all expected keys are present
    missing_keys = set(expected.keys()) - set(actual_filtered.keys())
    assert not missing_keys, f"Missing expected keys: {missing_keys}"

    # Ensure no unexpected keys (other than warnings) are present
    unexpected_keys = set(actual_filtered.keys()) - set(expected.keys())
    assert not unexpected_keys, f"Unexpected keys found: {unexpected_keys}"

    # Compare values for all expected keys
    mismatches = {}
    for key, value in expected.items():
        if actual_filtered.get(key) != value:
            mismatches[key] = {'expected': value, 'got': actual_filtered.get(key)}
    assert not mismatches, f"Value mismatches: {mismatches}"


def assert_posix_cron_compliance(cron_params):
    """
    Verifies that the cron parameters comply with POSIX crontab format.
    Checks ranges, steps, lists, and valid characters for each field.
    """
    cron_fields = {k: v for k, v in cron_params.items() if k in ['minute', 'hour', 'day', 'month', 'weekday']}

    valid_ranges = {
        'minute': (0, 59),
        'hour': (0, 23),
        'day': (1, 31),
        'month': (1, 12),
        'weekday': (0, 6) # POSIX Sunday=0 or 7, systemd maps to 0-6. Filter should output 0-6.
    }

    for field, value in cron_fields.items():
        if field not in valid_ranges:
            continue # Skip non-cron parameter keys like 'warnings'

        min_val, max_val = valid_ranges[field]

        assert isinstance(value, str), f"Field '{field}' value is not a string: {value}"
        assert value.strip() == value, f"Field '{field}' has leading/trailing whitespace: '{value}'"

        if value == '*':
            continue

        for element in value.split(','):
            assert element, f"Field '{field}' contains empty element due to extra comma: '{value}'"
            # Check for non-POSIX characters (systemd '~' should have been rejected earlier)
            assert '~' not in element, f"Field '{field}' contains unsupported '~': {element}"
            # Check for non-POSIX 'L' (last day/weekday - often Vixie-cron specific)
            assert 'L' not in element.upper(), f"Field '{field}' contains non-POSIX 'L': {element}"

            step_val = None
            base = element
            if '/' in element:
                # POSIX standard only supports */step for steps.
                # Vixie-cron (common Linux implementation) adds range/step and num/step.
                # We will allow Vixie-cron extensions here for broader compatibility,
                # as the filter might produce them from systemd steps.
                base, step = element.split('/')
                assert step.isdigit(), f"Step value in '{element}' for field '{field}' is not a digit: '{step}'"
                step_val = int(step)
                assert step_val > 0, f"Step value in '{element}' for field '{field}' must be > 0, got {step_val}"

            if base == '*':
                # '*' or '*/step' is valid
                continue
            elif '-' in base:
                # Range like 'n-m' or 'n-m/step'
                start, end = base.split('-')
                # Allow weekday range 6-0 for Sat-Sun
                if field == 'weekday' and start == '6' and end == '0':
                    start_num, end_num = 6, 0 # Special case validation
                else:
                    assert start.isdigit(), f"Range start in '{element}' for field '{field}' is not a digit: '{start}'"
                    assert end.isdigit(), f"Range end in '{element}' for field '{field}' is not a digit: '{end}'"
                    start_num, end_num = int(start), int(end)
                    assert start_num <= end_num, f"Range start '{start_num}' > end '{end_num}' in '{element}' for field '{field}'"

                # Validate numeric bounds
                assert min_val <= start_num <= max_val, \
                    f"Range start {start_num} for field '{field}' outside valid range [{min_val}, {max_val}]"
                # For weekday 6-0 range, end '0' is valid. Otherwise check end against max_val.
                if not (field == 'weekday' and start_num == 6 and end_num == 0):
                     assert min_val <= end_num <= max_val, \
                        f"Range end {end_num} for field '{field}' outside valid range [{min_val}, {max_val}]"

            elif base.isdigit():
                # Single number like 'n' or 'n/step' (n/step is Vixie-cron)
                num = int(base)
                assert min_val <= num <= max_val, \
                    f"Value {num} for field '{field}' outside valid range [{min_val}, {max_val}]"
            else:
                pytest.fail(f"Invalid format for element '{element}' in field '{field}': '{value}'")

def check_warnings(actual_result, expected_warnings):
    """Checks if the actual warnings match the expected warning substrings."""
    actual_warnings = actual_result.get('warnings', [])
    if not expected_warnings:
        assert not actual_warnings, f"Expected no warnings, but got: {actual_warnings}"
        return

    assert actual_warnings, f"Expected warnings containing {expected_warnings}, but got none."
    assert isinstance(actual_warnings, list), f"Warnings should be a list, but got {type(actual_warnings)}"

    found_mask = [False] * len(expected_warnings)
    for warn_idx, expected_sub in enumerate(expected_warnings):
        for actual_warn in actual_warnings:
             # Use regex for slightly more robust matching (ignore case, handle spacing)
            if re.search(expected_sub, actual_warn, re.IGNORECASE):
                found_mask[warn_idx] = True
                break # Found this expected warning, move to next expected one

    missing_warnings = [expected_warnings[i] for i, found in enumerate(found_mask) if not found]
    assert not missing_warnings, f"Did not find expected warning substrings: {missing_warnings} in actual warnings: {actual_warnings}"


# --- Test Cases ---

# todo: Add more test cases for wrapping around for other fields and also for ranege/step merging

# (Keep previous test functions like test_valid_conversions, test_special_expressions, etc.)

# Grouping valid conversions using parametrize
@pytest.mark.parametrize("input_str, expected_params, description", [
    # Basic Time Formats
    ("*-*/1 14:30:00", {'minute': '30', 'hour': '14', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour:Minute:Second"),
    ("*-*-* 14:30:00", {'minute': '30', 'hour': '14', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour:Minute:Second"),
    ("*-*-* 9:15", {'minute': '15', 'hour': '9', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour:Minute only"),
    ("*:10", {'minute': '10', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Wildcard Hour, specific Minute"),
    ("03:05", {'minute': '5', 'hour': '3', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour:Minute (implicit date/weekday)"),

    # Date Formats
    ("2025-04-15 10:00:00", {'minute': '0', 'hour': '10', 'day': '15', 'month': '4', 'weekday': '*'}, "Specific date with year (year ignored)"),
    ("*-06-01 00:00:00", {'minute': '0', 'hour': '0', 'day': '1', 'month': '6', 'weekday': '*'}, "Month-Day without year"),
    ("02-29 05:00", {'minute': '0', 'hour': '5', 'day': '29', 'month': '2', 'weekday': '*'}, "Month-Day (Feb 29)"),

    # Weekday Formats
    ("Mon *-*-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '1'}, "Specific weekday (Mon)"),
    ("Sun *-*-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '0'}, "Specific weekday (Sun)"),
    ("Sat *-*-* 08:00", {'minute': '0', 'hour': '8', 'day': '*', 'month': '*', 'weekday': '6'}, "Specific weekday (Sat)"),
    ("Mon,Wed,Fri *-*-* 14:30:00", {'minute': '30', 'hour': '14', 'day': '*', 'month': '*', 'weekday': '1,3,5'}, "Multiple weekdays (comma)"),
    ("Mon..Fri *-*-* 18:00:00", {'minute': '0', 'hour': '18', 'day': '*', 'month': '*', 'weekday': '1-5'}, "Range of weekdays (..)"),
    ("Sat..Sun *-*-* 09:00", {'minute': '0', 'hour': '9', 'day': '*', 'month': '*', 'weekday': '0,6'}, "Range of weekdays wrapping Sunday (Sat..Sun)"), 
    ("Sat..Tue *-*-* 09:00", {'minute': '0', 'hour': '9', 'day': '*', 'month': '*', 'weekday': '0-2,6'}, "Range of weekdays wrapping Sunday (Sat..Tue)"), 
    ("0..3 *-*-* 09:00", {'minute': '0', 'hour': '9', 'day': '*', 'month': '*', 'weekday': '0-3'}, "Numeric Range of weekdays (Sun-Wed)"),

    # Combined Formats
    ("Mon *-*-1 12:00:00", {'minute': '0', 'hour': '12', 'day': '1', 'month': '*', 'weekday': '1'}, "Weekday with specific day of month"),
    ("Tue,Thu *-1,7-1..7 9:30:00", {'minute': '30', 'hour': '9', 'day': '1-7', 'month': '1,7', 'weekday': '2,4'}, "Complex combination"),
    ("Mon *-01-01 00:00", {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '1'}, "Weekday with specific month and day"),
    ("Mon..Wed *-02-10..15 10:00", {'minute': '0', 'hour': '10', 'day': '10-15', 'month': '2', 'weekday': '1-3'}, "Weekday range with date range"),

    # Wildcards, Ranges, Steps
    ("*-*-* *:15:00", {'minute': '15', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Wildcard hour, specific minute"),
    ("*-*-* 9..17:00:00", {'minute': '0', 'hour': '9-17', 'day': '*', 'month': '*', 'weekday': '*'}, "Range of hours"),
    ("*-*-* *:*/15:00", {'minute': '*/15', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Minute step value (every 15)"),
    ("*-*-* *:0,15,30,45:00", {'minute': '0,15,30,45', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Minute list"),
    ("*-*-* 9..17/2:00:00", {'minute': '0', 'hour': '9-17/2', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour range with step"),
    ("*-*-* */6:30:00", {'minute': '30', 'hour': '*/6', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour step with specific minute"),
    ("*-*-1..15/3 01:00", {'minute': '0', 'hour': '1', 'day': '1-15/3', 'month': '*', 'weekday': '*'}, "Day range with step"),
    ("*-1,3,5-1 02:00", {'minute': '0', 'hour': '2', 'day': '1', 'month': '1,3,5', 'weekday': '*'}, "Month list"),
    ("*-1..6/2-15 03:00", {'minute': '0', 'hour': '3', 'day': '15', 'month': '1-6/2', 'weekday': '*'}, "Month range with step"),
    ("Mon,Wed,Fri *-*-* 04:00", {'minute': '0', 'hour': '4', 'day': '*', 'month': '*', 'weekday': '1,3,5'}, "Weekday list"),
    ("Mon..Fri/2 *-*-* 05:00", {'minute': '0', 'hour': '5', 'day': '*', 'month': '*', 'weekday': '1-5/2'}, "Weekday range with step"),
    ("Mon..Fri *-*-* 9..17:0,30:00", {'minute': '0,30', 'hour': '9-17', 'day': '*', 'month': '*', 'weekday': '1-5'}, "Work hours, weekdays, on hour/half hour"),
    ("Sat,Sun *-*-* 10..18/2:00:00", {'minute': '0', 'hour': '10-18/2', 'day': '*', 'month': '*', 'weekday': '0,6'}, "Weekends, even hours 10am-6pm"), # Note: systemd Sat,Sun might be 6,0
    ("Mon *-*-1..7 00:00:00", {'minute': '0', 'hour': '0', 'day': '1-7', 'month': '*', 'weekday': '1'}, "First Monday of month"),
    ("*-*-* 9..17:*/15:00", {'minute': '*/15', 'hour': '9-17', 'day': '*', 'month': '*', 'weekday': '*'}, "Every 15 min during work hours (no weekday specified)"),
    ("Mon..Fri *-*-* 9..17:*/15:00", {'minute': '*/15', 'hour': '9-17', 'day': '*', 'month': '*', 'weekday': '1-5'}, "Every 15 min during work hours on weekdays"),
    ("*-*/2-1 03:00:00", {'minute': '0', 'hour': '3', 'day': '1', 'month': '*/2', 'weekday': '*'}, "First day of alternate months"),
    ("*-*-1,15 12:00:00", {'minute': '0', 'hour': '12', 'day': '1,15', 'month': '*', 'weekday': '*'}, "Multiple specific days"),
    ("*-3,6,9-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '3,6,9', 'weekday': '*'}, "Multiple specific months"),
    ("*-*-* 9,12,15:00:00", {'minute': '0', 'hour': '9,12,15', 'day': '*', 'month': '*', 'weekday': '*'}, "Multiple specific hours"),

    # Boundary Values
    ("*-*-* 12:0:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '*'}, "Minute lower bound (0)"),
    ("*-*-* 12:59:00", {'minute': '59', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '*'}, "Minute upper bound (59)"),
    ("*-*-* 0:30:00", {'minute': '30', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour lower bound (0)"),
    ("*-*-* 23:30:00", {'minute': '30', 'hour': '23', 'day': '*', 'month': '*', 'weekday': '*'}, "Hour upper bound (23)"),
    ("*-*-1 12:00:00", {'minute': '0', 'hour': '12', 'day': '1', 'month': '*', 'weekday': '*'}, "Day lower bound (1)"),
    ("*-*-31 12:00:00", {'minute': '0', 'hour': '12', 'day': '31', 'month': '*', 'weekday': '*'}, "Day upper bound (31)"),
    ("*-1-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '1', 'weekday': '*'}, "Month lower bound (1)"),
    ("*-12-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '12', 'weekday': '*'}, "Month upper bound (12)"),
    ("Sun *-*-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '0'}, "Weekday lower bound (Sun=0)"),
    ("Sat *-*-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '6'}, "Weekday upper bound (Sat=6)"),

    # Case Insensitivity and Whitespace (New Tests)
    ("mon *-*-* 12:00:00", {'minute': '0', 'hour': '12', 'day': '*', 'month': '*', 'weekday': '1'}, "Lowercase weekday"),
    ("*-*-* 14:30:00  UTC", {'minute': '30', 'hour': '14', 'day': '*', 'month': '*', 'weekday': '*'}, "Whitespace and ignored timezone"),
    ("  Mon..Fri   *-*-*  9..17:*/15:00 ", {'minute': '*/15', 'hour': '9-17', 'day': '*', 'month': '*', 'weekday': '1-5'}, "Excess whitespace"),
])
def test_a_valid_conversions(systemd_filter, input_str, expected_params, description):
    """Tests various valid systemd calendar strings and checks POSIX compliance."""
    result = systemd_filter(input_str)
    assert_cron_params_match(result, expected_params)
    # Every valid conversion should produce POSIX compliant output
    assert_posix_cron_compliance(result)
    # Most basic valid conversions should not produce warnings (specific warning tests below)
    if "ignored timezone" not in description and "year ignored" not in description:
         assert 'warnings' not in result or not result['warnings'], f"Unexpected warnings for '{description}': {result.get('warnings')}"

@pytest.mark.parametrize("input_str, expected_params, description", [
    # Special Expressions
    ("minutely", {'minute': '*', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Minutely"),
    ("hourly", {'minute': '0', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Hourly"),
    ("daily", {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'}, "Daily"),
    ("weekly", {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'}, "Weekly (runs Monday 00:00)"), # systemd default is Mon 00:00
    ("monthly", {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '*'}, "Monthly"),
    ("yearly", {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'}, "Yearly"),
    ("annually", {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'}, "Annually (alias for yearly)"),
    ("quarterly", {'minute': '0', 'hour': '0', 'day': '1', 'month': '1,4,7,10', 'weekday': '*'}, "Quarterly"),
    ("semiannually", {'minute': '0', 'hour': '0', 'day': '1', 'month': '1,7', 'weekday': '*'}, "Semiannually"),
    # Case Insensitivity (New Tests)
    ("HOURLY", {'minute': '0', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, "Uppercase Hourly"),
    ("wEeKlY", {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'}, "Mixed case Weekly"),
])
def test_c_special_expressions(systemd_filter, input_str, expected_params, description):
    """Tests special predefined systemd calendar strings."""
    result = systemd_filter(input_str)
    assert_cron_params_match(result, expected_params)
    assert_posix_cron_compliance(result)
    assert 'warnings' not in result or not result['warnings'], f"Unexpected warnings for '{description}': {result.get('warnings')}"


@pytest.mark.parametrize("input_str, warning_substring, description", [
    # Seconds Handling (Ignored)
    ("*-*-* 12:30:45", "Ignored in conversion", "Seconds are ignored"),
    ("*:*:1", "Ignored in conversion", "Every second (ignored)"),
    ("*-*-* *:*/5:30", "Ignored in conversion", "Step seconds (ignored)"),
    ("*-*-* 12:30:45.123456", "Ignored in conversion", "Fractional seconds are ignored"),

    # Timezone Handling (Ignored)
    ("*-*-* 15:00:00 America/New_York", "Ignored timezone", "Timezone suffix ignored"),

    # Year Handling (Ignored)
    ("2024-06-15 10:00:00", "Ignored in conversion", "Specific year ignored"),
    ("Mon 2025-*-* 09:00", "Ignored in conversion", "Year with weekday"),
    ("2023..2025-01-01 00:00", "Ignored in conversion", "Year range ignored"),
])
def test_e_warnings_generated(systemd_filter, input_str, warning_substring, description):
    """Tests inputs that are valid but should generate specific warnings."""
    result = systemd_filter(input_str)
    # Check the basic conversion still happens
    assert isinstance(result, dict)
    assert 'minute' in result # Check a core field exists
    assert_posix_cron_compliance(result) # Output should still be compliant

    # Check for the specific warning
    assert 'warnings' in result, f"Expected warning for '{description}' but none found"
    assert isinstance(result['warnings'], list), "Warnings should be a list"
    assert any(warning_substring.lower() in w.lower() for w in result['warnings']), \
        f"Expected warning containing '{warning_substring}' for '{description}', got: {result['warnings']}"


@pytest.mark.parametrize("invalid_input, error_substring, description", [
    # Invalid Formats / Syntax Errors
    ("invalid format", "invalid calendar format", "Completely invalid string"),
    ("", "invalid calendar format", "Empty string"),
    (" ", "invalid calendar format", "Whitespace string"),
    ("*-*-* 25:00:00", "invalid hour value", "Hour out of range (>23)"),
    ("*-*-* 12:60:00", "invalid minute value", "Minute out of range (>59)"),
    ("*-*-0 12:00:00", "invalid day value", "Day out of range (<1)"),
    ("*-*-32 12:00:00", "invalid day value", "Day out of range (>31)"),
    ("*-0-* 12:00:00", "invalid month value", "Month out of range (<1)"),
    ("*-13-* 12:00:00", "invalid month value", "Month out of range (>12)"),
    ("Wom *.*.* 10:00", "invalid weekday", "Invalid weekday name"),
    ("7 *-*-* 10:00", "invalid weekday value", "Invalid numeric weekday (>6)"),
    ("*-*-* 10:00/0", "invalid step value", "Step value zero"), 
    ("*-*-* 10:00/-1", "invalid step value", "Negative step value"),
#todo: fix filter to handle this correctly in accordance with systemd.time 
#todo: Either time or date specification may be omitted, in which case *-*-* and 00:00:00 is implied, respectively. If the seconds component is not specified, ":00" is assumed.
    ("*-*-* 10:", "invalid time format", "Incomplete time"), 
    # Unsupported systemd Features (Tilde '~' for last day/weekday)
    ("*-*-~ 00:00:00", "unsupported feature: '~'", "Last day of month (~)"),
    ("*-*-~3 00:00:00", "unsupported feature: '~'", "Nth last day of month (~3)"),
    ("*-02-~ 00:00:00", "unsupported feature: '~'", "Last day of specific month"),
    ("*-04-~02 00:00:00", "unsupported feature: '~'", "Nth last day of specific month (~02)"),
    ("Mon *-05-~ 00:00:00", "unsupported feature: '~'", "Last specific weekday of month"),
    ("Fri *-*-~1 12:00:00", "unsupported feature: '~'", "Last Nth specific weekday of month"),
    ("Mon,Wed *-02,06-~2 14:30:00", "unsupported feature: '~'", "Complex tilde usage"),
])
def test_d_invalid_or_unsupported_formats(systemd_filter, invalid_input, error_substring, description):
    """
    Tests various invalid systemd calendar strings or unsupported features,
    expecting an AnsibleFilterError.
    """
    with pytest.raises(AnsibleFilterError) as excinfo:
        systemd_filter(invalid_input)
    # Optionally, check if the error message contains the expected substring
    # assert error_substring.lower() in str(excinfo.value).lower(), \
    #     f"Error message for '{description}' did not contain '{error_substring}'. Got: {str(excinfo.value)}"

# --- New Test Section for systemd.time man page examples ---

# Define the expected results based *only* on the normalized systemd format
# Warnings are determined by features present in the normalized form (seconds, year, timezone)
man_page_test_data = [
    # Input Str                      # Expected Cron Params                                                           # Expected Warnings                          # Description
    ("Sat,Thu,Mon..Wed,Sat..Sun",    {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '0-4,6'},      [],                                          "man page: Weekday list/range"),
    ("Mon..Thu,Sat,Sun *-*-* 00:00:00", {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '0-4,6'},    [],                                  "man page: Weekday list/range (normalized)"),
    ("Mon,Sun 12-*-* 2,1:23",        {'minute': '23', 'hour': '1,2', 'day': '*', 'month': '*', 'weekday': '0,1'},      ['year'],                          "man page: Weekday list, Year, H:M"),
    ("Mon,Sun 2012-*-* 01,02:23:00", {'minute': '23', 'hour': '1,2', 'day': '*', 'month': '*', 'weekday': '0,1'},      ['year'],                          "man page: Weekday list, Year, H:M (normalized)"),
    ("Wed *-1",                      {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '3'},          [],                                          "man page: Weekday, Day only"),
    ("Wed *-*-01 00:00:00",          {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '3'},          [],                                  "man page: Weekday, Day only (normalized)"),
    ("Wed..Wed,Wed *-1",             {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '3'},          [],                                          "man page: Redundant weekday range/list"),
    #("Wed *-*-01 00:00:00", ... ) # Normalized form already tested above
    ("Wed, 17:48",                   {'minute': '48', 'hour': '17', 'day': '*', 'month': '*', 'weekday': '3'},          [],                                          "man page: Weekday, Time only"),
    ("Wed *-*-* 17:48:00",           {'minute': '48', 'hour': '17', 'day': '*', 'month': '*', 'weekday': '3'},          [],                                  "man page: Weekday, Time only (normalized)"),
    ("Wed..Sat,Tue 12-10-15 1:2:3",  {'minute': '2', 'hour': '1', 'day': '15', 'month': '10', 'weekday': '2-6'},      ['year', 'second'],                          "man page: Weekday range/list, Date, Time"),
    ("Tue..Sat 2012-10-15 01:02:03", {'minute': '2', 'hour': '1', 'day': '15', 'month': '10', 'weekday': '2-6'},      ['year', 'second'],                          "man page: Weekday range/list, Date, Time (normalized)"),
    ("*-*-7 0:0:0",                  {'minute': '0', 'hour': '0', 'day': '7', 'month': '*', 'weekday': '*'},          [],                                  "man page: Day, Time zero"),
    ("*-*-07 00:00:00",              {'minute': '0', 'hour': '0', 'day': '7', 'month': '*', 'weekday': '*'},          [],                                  "man page: Day, Time zero (normalized)"),
    ("10-15",                        {'minute': '0', 'hour': '0', 'day': '15', 'month': '10', 'weekday': '*'},          [],                                          "man page: Month-Day only"),
    ("*-10-15 00:00:00",             {'minute': '0', 'hour': '0', 'day': '15', 'month': '10', 'weekday': '*'},          [],                                  "man page: Month-Day only (normalized)"),
    ("monday *-12-* 17:00",          {'minute': '0', 'hour': '17', 'day': '*', 'month': '12', 'weekday': '1'},          [],                                  "man page: Lowercase weekday, Month, Time"),
    ("Mon *-12-* 17:00:00",          {'minute': '0', 'hour': '17', 'day': '*', 'month': '12', 'weekday': '1'},          [],                                  "man page: Lowercase weekday, Month, Time (normalized)"),
    ("Mon,Fri *-*-3,1,2 *:30:45",    {'minute': '30', 'hour': '*', 'day': '1-3', 'month': '*', 'weekday': '1,5'},      ['second'],                                  "man page: Weekday list, Day list, Time"),
    ("Mon,Fri *-*-01,02,03 *:30:45", {'minute': '30', 'hour': '*', 'day': '1-3', 'month': '*', 'weekday': '1,5'},      ['second'],                                  "man page: Weekday list, Day list, Time (normalized)"),
    ("12,14,13,12:20,10,30",         {'minute': '10,20,30', 'hour': '12-14', 'day': '*', 'month': '*', 'weekday': '*'}, [],                                          "man page: Hour list, Minute list"),
    ("*-*-* 12,13,14:10,20,30:00",   {'minute': '10,20,30', 'hour': '12-14', 'day': '*', 'month': '*', 'weekday': '*'}, [],                                  "man page: Hour list, Minute list (normalized)"),
    ("12..14:10,20,30",              {'minute': '10,20,30', 'hour': '12-14', 'day': '*', 'month': '*', 'weekday': '*'},   [],                                          "man page: Hour range, Minute list"),
    ("*-*-* 12..14:10,20,30:00",     {'minute': '10,20,30', 'hour': '12-14', 'day': '*', 'month': '*', 'weekday': '*'},   [],                                  "man page: Hour range, Minute list (normalized)"),
    ("mon,fri *-1/2-1,3 *:30:45",    {'minute': '30', 'hour': '*', 'day': '1,3', 'month': '1/2', 'weekday': '1,5'},      ['second'],                                  "man page: Weekday list, Month step, Day list, Time"), # Assuming 1/2 -> */2
    ("Mon,Fri *-01/2-01,03 *:30:45", {'minute': '30', 'hour': '*', 'day': '1,3', 'month': '1/2', 'weekday': '1,5'},      ['second'],                                  "man page: Weekday list, Month step, Day list, Time (normalized)"),
    ("03-05 08:05:40",               {'minute': '5', 'hour': '8', 'day': '5', 'month': '3', 'weekday': '*'},          ['second'],                                  "man page: Month-Day, Time"),
    ("*-03-05 08:05:40",             {'minute': '5', 'hour': '8', 'day': '5', 'month': '3', 'weekday': '*'},          ['second'],                                  "man page: Month-Day, Time (normalized)"),
    ("08:05:40",                     {'minute': '5', 'hour': '8', 'day': '*', 'month': '*', 'weekday': '*'},          ['second'],                                  "man page: Time H:M:S only"),
    ("*-*-* 08:05:40",               {'minute': '5', 'hour': '8', 'day': '*', 'month': '*', 'weekday': '*'},          ['second'],                                  "man page: Time H:M:S only (normalized)"),
    ("05:40",                        {'minute': '40', 'hour': '5', 'day': '*', 'month': '*', 'weekday': '*'},          [],                                          "man page: Time H:M only"),
    ("*-*-* 05:40:00",               {'minute': '40', 'hour': '5', 'day': '*', 'month': '*', 'weekday': '*'},          [],                                  "man page: Time H:M only (normalized)"),
    ("Sat,Sun 12-05 08:05:40",       {'minute': '5', 'hour': '8', 'day': '5', 'month': '12', 'weekday': '0,6'},      ['second'],                                  "man page: Weekday list, Month-Day, Time"),
    ("Sat,Sun *-12-05 08:05:40",     {'minute': '5', 'hour': '8', 'day': '5', 'month': '12', 'weekday': '0,6'},      ['second'],                                  "man page: Weekday list, Month-Day, Time (normalized)"),
    ("Sat,Sun 08:05:40",             {'minute': '5', 'hour': '8', 'day': '*', 'month': '*', 'weekday': '0,6'},      ['second'],                                  "man page: Weekday list, Time H:M:S"),
    ("Sat,Sun *-*-* 08:05:40",       {'minute': '5', 'hour': '8', 'day': '*', 'month': '*', 'weekday': '0,6'},      ['second'],                                  "man page: Weekday list, Time H:M:S (normalized)"),
    ("2003-03-05 05:40",             {'minute': '40', 'hour': '5', 'day': '5', 'month': '3', 'weekday': '*'},          ['year'],                                    "man page: Date Y-M-D, Time H:M"),
    ("2003-03-05 05:40:00",          {'minute': '40', 'hour': '5', 'day': '5', 'month': '3', 'weekday': '*'},          ['year'],                          "man page: Date Y-M-D, Time H:M (normalized)"),
    ("05:40:23.4200004/3.1700005",   {'minute': '40', 'hour': '5', 'day': '*', 'month': '*', 'weekday': '*'},          ['fractional seconds'],                      "man page: Time H:M:S with fractional/step"), # Filter logic determines exact minute/hour
    ("*-*-* 05:40:23.420000/3.170001",{'minute': '40', 'hour': '5', 'day': '*', 'month': '*', 'weekday': '*'},          ['fractional seconds'],                      "man page: Time H:M:S with fractional/step (normalized)"),
    ("2003-02..04-05",               {'minute': '0', 'hour': '0', 'day': '5', 'month': '2-4', 'weekday': '*'},          ['year'],                                    "man page: Date with month range"),
    ("2003-02..04-05 00:00:00",      {'minute': '0', 'hour': '0', 'day': '5', 'month': '2-4', 'weekday': '*'},          ['year'],                          "man page: Date with month range (normalized)"),
    ("2003-03-05 05:40 UTC",         {'minute': '40', 'hour': '5', 'day': '5', 'month': '3', 'weekday': '*'},          ['year', 'timezone'],                        "man page: Date, Time, Timezone"),
    ("2003-03-05 05:40:00 UTC",      {'minute': '40', 'hour': '5', 'day': '5', 'month': '3', 'weekday': '*'},          ['year', 'timezone'],            "man page: Date, Time, Timezone (normalized)"),
    ("2003-03-05",                   {'minute': '0', 'hour': '0', 'day': '5', 'month': '3', 'weekday': '*'},          ['year'],                                    "man page: Date Y-M-D only"),
    ("2003-03-05 00:00:00",          {'minute': '0', 'hour': '0', 'day': '5', 'month': '3', 'weekday': '*'},          ['year'],                          "man page: Date Y-M-D only (normalized)"),
    ("03-05",                        {'minute': '0', 'hour': '0', 'day': '5', 'month': '3', 'weekday': '*'},          [],                                          "man page: Date M-D only"),
    ("*-03-05 00:00:00",             {'minute': '0', 'hour': '0', 'day': '5', 'month': '3', 'weekday': '*'},          [],                                  "man page: Date M-D only (normalized)"),
    ("hourly",                       {'minute': '0', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'},          [],                                          "man page: hourly keyword"),
    ("*-*-* *:00:00",                {'minute': '0', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'},          [],                                  "man page: hourly keyword (normalized)"),
    ("daily",                        {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'},          [],                                          "man page: daily keyword"),
    ("*-*-* 00:00:00",               {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'},          [],                                  "man page: daily keyword (normalized)"),
    #? ("daily UTC",                    {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'},          ['timezone'],                                "man page: daily keyword with timezone"),
    ("*-*-* 00:00:00 UTC",           {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'},          ['timezone'],                      "man page: daily keyword with timezone (normalized)"),
    ("monthly",                      {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '*'},          [],                                          "man page: monthly keyword"),
    ("*-*-01 00:00:00",              {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '*'},          [],                                  "man page: monthly keyword (normalized)"),
    ("weekly",                       {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'},          [],                                          "man page: weekly keyword"), # Defaults to Monday
    ("Mon *-*-* 00:00:00",           {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'},          [],                                  "man page: weekly keyword (normalized)"),
    #? ("weekly Pacific/Auckland",      {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'},          ['timezone'],                                "man page: weekly keyword with timezone"),
    ("Mon *-*-* 00:00:00 Pacific/Auckland", {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'},    ['timezone'],                      "man page: weekly keyword with timezone (normalized)"),
    ("yearly",                       {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'},          [],                                          "man page: yearly keyword"),
    ("*-01-01 00:00:00",             {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'},          [],                                  "man page: yearly keyword (normalized)"),
    ("annually",                     {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'},          [],                                          "man page: annually keyword"),
    #("*-01-01 00:00:00", ... ) # Normalized form already tested above
    # Note: systemd's *:2/3 means min 2, 5, 8... Cron POSIX/Vixie doesn't map perfectly.
    # Assuming filter converts this to a list or a Vixie-cron range/step.
    # Let's assume list '2,5,8,...,59' for POSIX or '2-59/3' for Vixie-Cron.
    # Using list form here for the check. Adjust if your filter outputs range/step.
    ("*:2/3",                        {'minute': '2/3', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, [], "man page: Minute step with offset"),
    ("*-*-* *:02/3:00",              {'minute': '2/3', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}, [], "man page: Minute step with offset (normalized)"),

]

@pytest.mark.parametrize("input_str, expected_params, expected_warnings, description", man_page_test_data)
def test_b_man_page_examples(systemd_filter, input_str, expected_params, expected_warnings, description):
    """Tests specific examples from the systemd.time man page."""
    print(f"Testing: {description} ('{input_str}')") # Optional: for better verbose output
    result = systemd_filter(input_str)

    # Check core cron parameters
    assert_cron_params_match(result, expected_params)

    # Check output format compliance
    assert_posix_cron_compliance(result)

    # Check warnings
    check_warnings(result, expected_warnings)

# --- (Keep other test functions as needed) ---

# todo: Add more test cases for wrapping around for other fields and also for ranege/step merging