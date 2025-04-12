#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import traceback

from ansible.errors import AnsibleFilterError, AnsibleError
from ansible.module_utils.common.text.converters import to_native, to_text

DOCUMENTATION = r'''
---
name: systemd_to_cron
author: Marek Bielik, with major assistance from Claude 3.7 Sonnet.
short_description: Converts systemd timer formats to Ansible cron parameters
description:
    - Takes a systemd.time(7) calendar event expression.
    - Returns a dictionary of parameters for Ansible's cron module.
    - Handles complex time specifications,special time keywords, weekday ranges, and repetition steps.
    - Ignores parameters not supported in crontab (e.g. seconds, years, timezones)
    - and includes a warning with stripped out unsupported parameters in the result.
    - Returns error if feature not supported by cron (e.g. '~' for last day of month).
      
options:
    _input:
        description: The systemd.time(7) calendar event expression to convert
        required: true
        type: str
'''

EXAMPLES = r'''
# Convert special keywords
- name: Set up daily job
  vars:
    cron_params: "{{ 'daily' | systemd_to_cron }}"
  cron:
    name: "Daily backup"
    job: "/usr/local/bin/backup.sh"
    minute: "{{ cron_params.minute }}"
    hour: "{{ cron_params.hour }}"
    day: "{{ cron_params.day }}"
    month: "{{ cron_params.month }}"
    weekday: "{{ cron_params.weekday }}"

# Convert weekday and time specification
- name: Set up workday job
  vars:
    cron_params: "{{ 'Mon..Fri *-*-* 17:00:00' | systemd_to_cron }}"
  cron:
    name: "Workday reminder"
    job: "/usr/local/bin/remind.sh"
    minute: "{{ cron_params.minute }}"
    hour: "{{ cron_params.hour }}"
    day: "{{ cron_params.day }}"
    month: "{{ cron_params.month }}"
    weekday: "{{ cron_params.weekday }}"
'''

RETURN = r'''
dict:
    description: Dictionary containing cron module parameters
    type: dict
    contains:
        minute:
            description: Minute field for cron
            type: str
            sample: "0"
        hour:
            description: Hour field for cron
            type: str
            sample: "*/2"
        day:
            description: Day of month field for cron
            type: str
            sample: "1,15"
        month:
            description: Month field for cron
            type: str
            sample: "*"
        weekday:
            description: Day of week field for cron
            type: str
            sample: "1-5"
        warnings:
            description: List of warnings about conversions that might not be exact
            type: list
            elements: str
            sample: ["Seconds specification not supported in standard cron. Ignored in conversion."]
        error:
            description: Error message in case of conversion failure
            type: str
            sample: "Error converting systemd timer: Invalid syntax"
'''

# Map systemd weekday names to cron weekday numbers (0-6, where 0=Sunday)
WEEKDAY_MAP = {
    'mon': '1', 'monday': '1',
    'tue': '2', 'tuesday': '2',
    'wed': '3', 'wednesday': '3',
    'thu': '4', 'thursday': '4',
    'fri': '5', 'friday': '5',
    'sat': '6', 'saturday': '6',
    'sun': '0', 'sunday': '0'
}

# Special time keywords mapping to cron parameters
SPECIAL_KEYWORDS = {
    'minutely': {'minute': '*', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'},
    'hourly': {'minute': '0', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'},
    'daily': {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '*'},
    'weekly': {'minute': '0', 'hour': '0', 'day': '*', 'month': '*', 'weekday': '1'},
    'monthly': {'minute': '0', 'hour': '0', 'day': '1', 'month': '*', 'weekday': '*'},
    'yearly': {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'},
    'annually': {'minute': '0', 'hour': '0', 'day': '1', 'month': '1', 'weekday': '*'},
    'quarterly': {'minute': '0', 'hour': '0', 'day': '1', 'month': '1,4,7,10', 'weekday': '*'},
    'semiannually': {'minute': '0', 'hour': '0', 'day': '1', 'month': '1,7', 'weekday': '*'}
}


def _strip_leading_zeros(value):
    """
    Strip leading zeros from numeric values, preserving special cron characters
    
    Args:
        value (str): A string value that might have leading zeros
        
    Returns:
        str: The value with leading zeros removed from numeric parts
    """
    if not value or not isinstance(value, str):
        return value
    
    # Only process if it's a simple number (not a range, list, or pattern)
    if value.isdigit():
        return str(int(value))
    
    # Handle comma-separated lists
    if ',' in value:
        parts = value.split(',')
        return ','.join(_strip_leading_zeros(part) for part in parts)
    
    # Handle ranges with steps (n-m/s)
    if '-' in value and '/' in value:
        range_part, step = value.split('/')
        start, end = range_part.split('-')
        return f"{_strip_leading_zeros(start)}-{_strip_leading_zeros(end)}/{_strip_leading_zeros(step)}"
    
    # Handle ranges (n-m)
    if '-' in value:
        start, end = value.split('-')
        return f"{_strip_leading_zeros(start)}-{_strip_leading_zeros(end)}"
    
    # Handle steps (*/s or n/s)
    if '/' in value:
        base, step = value.split('/')
        if base != '*':
            base = _strip_leading_zeros(base)
        return f"{base}/{_strip_leading_zeros(step)}"
    
    # Just return the value if it doesn't match any pattern
    return value


def _validate_cron_component(component, field_name, min_val, max_val):
    """
    Validate individual cron component values with proper support for
    complex expressions like ranges with steps (1-5/2).
    """
    if component == '*':
        return
    
    # First split by commas to get individual expressions
    for expr in component.split(','):
        # Handle different expression patterns
        if expr == '*':
            continue
            
        # Handle range with step (e.g., 1-5/2)
        elif '-' in expr and '/' in expr:
            range_part, step = expr.split('/')
            start, end = range_part.split('-')
            
            # Validate each numerical component
            for val in [start, end, step]:
                if not val.isdigit():
                    raise AnsibleFilterError(
                        f"Invalid {field_name} value: {val} in {expr}. "
                        f"Must be a number between {min_val}-{max_val}."
                    )
                num_val = int(val)
                if val != step and not (min_val <= num_val <= max_val):
                    raise AnsibleFilterError(
                        f"Invalid {field_name} value: {num_val} in {expr}. "
                        f"Must be between {min_val}-{max_val}."
                    )
                
            # Make sure start <= end
            if int(start) > int(end):
                raise AnsibleFilterError(
                    f"Invalid range in {field_name}: {expr}. Start must be <= end."
                )
                
        # Handle step (e.g., */2 or 1/3)
        elif '/' in expr:
            base, step = expr.split('/')

#!  Add support for step validation, can't be less than 1 and more than max_val            
            # Validate step value
            if not step.isdigit() or int(step) < 1 or int(step) > max_val:
                raise AnsibleFilterError(
                    f"Invalid step value in {field_name}: {step}. "
                    f"Must be a number between 1 and {max_val}."
                )
                
            # Validate base value if not '*'
            if base != '*':
                if not base.isdigit():
                    raise AnsibleFilterError(
                        f"Invalid {field_name} value: {base} in {expr}. "
                        f"Must be a number between {min_val}-{max_val} or '*'."
                    )
                base_val = int(base)
                if not (min_val <= base_val <= max_val):
                    raise AnsibleFilterError(
                        f"Invalid {field_name} value: {base_val}. "
                        f"Must be between {min_val}-{max_val}."
                    )
                    
        # Handle range (e.g., 1-5)
        elif '-' in expr:
            start, end = expr.split('-')
            
            # Validate start and end values
            for val in [start, end]:
                if not val.isdigit():
                    raise AnsibleFilterError(
                        f"Invalid {field_name} value: {val} in {expr}. "
                        f"Must be a number between {min_val}-{max_val}."
                    )
                num_val = int(val)
                if not (min_val <= num_val <= max_val):
                    raise AnsibleFilterError(
                        f"Invalid {field_name} value: {num_val}. "
                        f"Must be between {min_val}-{max_val}."
                    )
            
#! Kept for reference, disabling cause cron supports ranges with start > end
            # # Make sure start <= end
            # if int(start) > int(end):
            #     raise AnsibleFilterError(
            #         f"Invalid range in {field_name}: {expr}. Start must be <= end."
            #     ) 

        # Handle simple value (e.g., 5)
        else:
            if not expr.isdigit():
                raise AnsibleFilterError(
                    f"Invalid {field_name} value: {expr}. "
                    f"Must be a number between {min_val}-{max_val}."
                )
                
            val = int(expr)
            if not (min_val <= val <= max_val):
                raise AnsibleFilterError(
                    f"Invalid {field_name} value: {val}. "
                    f"Must be between {min_val}-{max_val}."
                )

def _parse_time_component(component, default='*'):
    """
    Parse a time component from systemd format to cron format
    
    Args:
        component (str): The component from systemd timer format
        default (str): Default value if component is empty or None
        
    Returns:
        str: Converted component in cron format
    """
    if not component:
        return default
    
    # Handle "00" values correctly
    if component == '00':
        return '0'
    
    if component == '*':
        return '*'
    
    # Handle comma-separated values and ranges
    parts = []
    for part in component.split(','):
        if '..' in part:
            range_parts = part.split('..')
            start = range_parts[0]
            end_parts = range_parts[1].split('/')
            end = end_parts[0]
            
            # Convert "00" to "0" within ranges and strip leading zeros
            if start == '00':
                start = '0'
            else:
                start = _strip_leading_zeros(start)
                
            if end == '00':
                end = '0'
            else:
                end = _strip_leading_zeros(end)
            
            # Handle ranges with steps
            if len(end_parts) > 1:
                step = end_parts[1]
                step = _strip_leading_zeros(step)
                # Cron uses dash for ranges, not ..
                parts.append(f"{start}-{end}/{step}")
            else:
                parts.append(f"{start}-{end}")
        elif '/' in part:
            base_step = part.split('/')
            base = base_step[0]
            step = base_step[1]
            
            # Convert "00" to "0" in base and strip leading zeros
            if base == '00':
                base = '0'
            elif base.isdigit():
                base = _strip_leading_zeros(base)
            
            step = _strip_leading_zeros(step)
            
            # Handle steps
            if base == '*':
                # */1 is equivalent to * in cron
                if step == '1':
                    parts.append('*')
                else:
                    parts.append(f"*/{step}")
            else:
                parts.append(f"{base}/{step}")
        else:
            # Convert "00" to "0" for individual values and strip leading zeros
            if part == '00':
                parts.append('0')
            elif part.isdigit():
                parts.append(_strip_leading_zeros(part))
            else:
                parts.append(part)
    
    return ','.join(parts)


def _parse_weekday(weekday_spec):
    """
    Parse and validate weekday specification with support for steps
    """
    if not weekday_spec or weekday_spec == '*':
        return '*'

    result = []
    parts = re.split(r'[\s,]+', weekday_spec.lower())
    
    for part in parts:
        # Handle range with step (Mon..Fri/2)
        if '..' in part and '/' in part:
            # Split out the step first
            range_part, step = part.split('/', 1)
            # Then split the range
            start, end = range_part.split('..', 1)
            
            # Validate step
            if not step.isdigit() or int(step) < 1:
                raise AnsibleFilterError(
                    f"Invalid step value: {step} in {part}. "
                    "Must be a positive number."
                )
            
            # Validate start and end weekdays
            start_num = WEEKDAY_MAP.get(start.lower(), start)
            end_num = WEEKDAY_MAP.get(end.lower(), end)
            
            if not start_num.isdigit() or not 0 <= int(start_num) <= 6:
                raise AnsibleFilterError(
                    f"Invalid weekday: {start} in {part}. "
                    "Use valid weekday names (Mon-Sun) or numbers 0-6."
                )
            
            if not end_num.isdigit() or not 0 <= int(end_num) <= 6:
                raise AnsibleFilterError(
                    f"Invalid weekday: {end} in {part}. "
                    "Use valid weekday names (Mon-Sun) or numbers 0-6."
                )
            
            # Add the range with step to result
            result.append(f"{start_num}-{end_num}/{step}")
            
        # Handle simple range (Mon..Fri)
        elif '..' in part:
            start, end = part.split('..', 1)
            
            # Validate start and end weekdays
            start_num = WEEKDAY_MAP.get(start.lower(), start)
            end_num = WEEKDAY_MAP.get(end.lower(), end)
            
            if not start_num.isdigit() or not 0 <= int(start_num) <= 6:
                raise AnsibleFilterError(
                    f"Invalid weekday: {start} in {part}. "
                    "Use valid weekday names (Mon-Sun) or numbers 0-6."
                )
            
            if not end_num.isdigit() or not 0 <= int(end_num) <= 6:
                raise AnsibleFilterError(
                    f"Invalid weekday: {end} in {part}. "
                    "Use valid weekday names (Mon-Sun) or numbers 0-6."
                )
            
            # Add the range to result
            result.append(f"{start_num}-{end_num}")
            
        # Handle individual weekday
        else:
            # Skip part if empty
            if not part:
                continue
            # Validate weekday
            if part not in WEEKDAY_MAP and not (part.isdigit() and 0 <= int(part) <= 6):
                raise AnsibleFilterError(
                    f"Invalid weekday specification: {part}. "
                    "Use valid weekday names (Mon-Sun) or numbers 0-6."
                )
            
            mapped = WEEKDAY_MAP.get(part, part)
            result.append(mapped)

    # Join the parts with commas
    cron_weekday = ','.join(result)
    
    return cron_weekday

def _parse_systemd_calendar(calendar_spec):
    """
    Parse a systemd calendar specification into component parts
    
    Args:
        calendar_spec (str): The systemd calendar specification
        
    Returns:
        dict: Dictionary with 'weekday', 'date', 'time', and 'timezone' components
    """
    # Initialize with default values
    result = {
        'weekday': '*',
        'date': '*-*-*',
        'time': '00:00:00',
        'timezone': None
    }

    
    # Split the calendar specification into parts
    parts = re.split(r'\s+', calendar_spec.strip())
    if not parts:
        return result
    
    # Check for timezone at the end
    if parts[-1] in ['UTC', 'GMT'] or (re.match(r'^[A-Za-z]+/[A-Za-z_]+$', parts[-1]) and len(parts) > 1):
        result['timezone'] = parts.pop()
    
    # Find the time component (contains :)
    time_index = -1
    for i, part in enumerate(parts):
        if ':' in part:
            result['time'] = part
            time_index = i
            break
    
    # Remove the time component if found
    if time_index >= 0:
        parts.pop(time_index)
    
    # Find the date component (contains -)
    date_index = -1
    for i, part in enumerate(parts):
        if '-' in part or part.isdigit():
            # If it's just a number, it's interpreted as the day of month
            if part.isdigit():
                result['date'] = f"*-*-{part}"
            else:
                result['date'] = part
            date_index = i
            break
    
    # Remove the date component if found
    if date_index >= 0:
        parts.pop(date_index)

#? remove normalization - shoulnd't rather raise an error?
    # Normalize date format
    date = result['date']
    if re.match(r'^\d+$', date):
        result['date'] = f"*-*-{date}"
    elif date.count('-') == 1:
        result['date'] = f"*-{date}"

#? remove normalization shoulnd't rather raise an error?    
    # Normalize time format
    time = result['time']
    if time.count(':') == 1:
        result['time'] = f"{time}:00"
    
    # Any remaining parts are weekday specifications
    if parts:
        result['weekday'] = ' '.join(parts)
    
    return result


def _parse_date_components(date_str):
    """
    Parse the date part of systemd calendar specification
    
    Args:
        date_str (str): Date specification from systemd timer
        
    Returns:
        tuple: (year, month, day, warnings_list)
    """
    warnings = []
    
    if date_str == '*-*-*':
        return '*', '*', '*', warnings
    
    # Split into year, month, day
    parts = date_str.split('-')
    year = parts[0] if len(parts) > 0 else '*'
    month = parts[1] if len(parts) > 1 else '*'
    day = parts[2] if len(parts) > 2 else '*'
    
    # Handle last day of month
    if '~' in day:
        day, day_warnings = _handle_last_day_of_month(day)
        warnings.extend(day_warnings)
    
    # Convert components to cron format - strip leading zeros
    if month != '*':
        month = _parse_time_component(month)
        # Ensure no leading zeros for month
        if month.isdigit():
            month = str(int(month))
    
    # If day is not the special 'L', parse it and ensure no leading zeros
    if day != 'L' and day != '*':
        day = _parse_time_component(day)
        if day.isdigit():
            day = str(int(day))
    
    # Year is not supported in standard cron
    if year != '*':
        warnings.append(
            f"Year specification '{year}' not supported in standard cron. "
            "Ignored in conversion."
        )
    
    return year, month, day, warnings


def _parse_time_components(time_str):
    """
    Parse the time part of systemd calendar specification
    
    Args:
        time_str (str): Time specification from systemd timer
        
    Returns:
        tuple: (hour, minute, second, warnings_list)
    """
    warnings = []
    
    # Split into hour, minute, second
    parts = time_str.split(':')
    hour = parts[0] if len(parts) > 0 else '0'
    minute = parts[1] if len(parts) > 1 else '0'
    second = parts[2] if len(parts) > 2 else '0'
    
    # Handle "00" values correctly - in cron, should be "0" not "00"
    if hour == '00':
        hour = '0'
    elif hour.isdigit():
        hour = str(int(hour))  # Strip leading zeros
        
    if minute == '00':
        minute = '0'
    elif minute.isdigit():
        minute = str(int(minute))  # Strip leading zeros
        
    if second == '00':
        second = '0'
    elif second.isdigit() and second != '0':
        second = str(int(second))  # Strip leading zeros
    
    # Convert to cron format - preserving the distinction between * and 0
    if hour != '*':
        hour = _parse_time_component(hour, default='0')
    if minute != '*':
        minute = _parse_time_component(minute, default='0')
    
    # Handle decimal fractions in seconds
    if '.' in second:
        second_parts = second.split('.')
        second = second_parts[0]
        warnings.append(
            f"Fractional seconds '{time_str}' not supported in standard cron. "
            f"Truncated to whole seconds."
        )
    
    # Standard cron doesn't support seconds
    if second != '0':
        warnings.append(
            f"Seconds specification '{second}' not supported in standard cron. "
            "Ignored in conversion."
        )
    
    return hour, minute, second, warnings

def _simplify_crontab(expr, field_name, min_val, max_val):
    """
    Simplify a crontab-like expression by combining adjacent or overlapping ranges.
    Preserves step notation and validates step values.
    
    Args:
        expr (str): A string representing values and ranges, e.g., '6,4,1-3,6-0' or '9-17/2'.
        min_val (int): The minimum value in the valid range (default 0).
        max_val (int): The maximum value in the valid range (default 59).
        
    Returns:
        str: A simplified expression with preserved valid step notation.
    """
    # Handle wildcard and empty cases
    if expr == '*':
        return '*'
    if not expr:
        return ""
    
    parts = expr.split(',')
    
    # Check if any part is a wildcard
    if '*' in parts:
        return '*'
    
    # Sort parts into step-based and non-step based
    step_parts = []
    regular_parts = []
    
    for part in parts:
        if '/' in part:
            # This part uses step notation - validate it
            range_part, step_str = part.split('/')
            
            try:
                step = int(step_str)
                # Validate step value
                if step <= 1 or step > max_val:
                    # Invalid step value - treat as regular part without step
                    if range_part == '*':
                        # */1 is equivalent to *
                        return '*'
                    else:
                        regular_parts.append(range_part)
                    continue
                
                # Valid step - continue processing
                if range_part == '*':
                    step_parts.append(part)
                elif '-' in range_part:
                    start, end = map(int, range_part.split('-'))
                    # Handle wraparound
                    if start > end:
                        # Split wraparound range with step into two parts
                        if start <= max_val:
                            step_parts.append(f"{start}-{max_val}/{step}")
                        if min_val <= end:
                            step_parts.append(f"{min_val}-{end}/{step}")
                    else:
                        step_parts.append(part)
                else:
                    # Single value with step
                    try:
                        val = int(range_part)
                        if min_val <= val <= max_val:
                            step_parts.append(part)
                    except ValueError:
                        continue
            except ValueError:
                # Invalid step format - treat as regular part
                regular_parts.append(range_part)
        else:
            regular_parts.append(part)
    
    # Process regular parts as before
    regular_values = set()
    
    for part in regular_parts:
        if '-' in part:
            start, end = map(int, part.split('-'))
            if start <= end:
                regular_values.update(range(start, end + 1))
            else:
                # Wraparound range
                regular_values.update(range(start, max_val + 1))
                regular_values.update(range(min_val, end + 1))
        else:
            # Single value
            try:
                regular_values.add(int(part))
            except ValueError:
                continue
    
    # Find continuous ranges in regular values
    sorted_regular = sorted(regular_values)
    regular_ranges = []
    
    if sorted_regular:
        i = 0
        while i < len(sorted_regular):
            start = sorted_regular[i]
            j = i
            while j + 1 < len(sorted_regular) and sorted_regular[j + 1] == sorted_regular[j] + 1:
                j += 1
            end = sorted_regular[j]
            
            if start == end:
                # Single value
                regular_ranges.append(str(start))
            elif end - start == 1:
                # Exactly two adjacent values - use comma notation
                regular_ranges.append(f"{start},{end}")
            else:
                # Range with three or more values - use hyphen notation
                regular_ranges.append(f"{start}-{end}")
            
            i = j + 1
    
    # Check if all possible values are included
    all_values = set(regular_values)
    for part in step_parts:
        range_part, step_str = part.split('/')
        step = int(step_str)
        
        if range_part == '*':
            all_values.update(range(min_val, max_val + 1, step))
        elif '-' in range_part:
            start, end = map(int, range_part.split('-'))
            if start <= end:
                all_values.update(range(start, end + 1, step))
            else:
                # Wraparound - already split into two parts in previous processing
                pass
        else:
            # Single value with step
            val = int(range_part)
            if min_val <= val <= max_val:
                all_values.add(val)
                current = val + step
                while current <= max_val:
                    all_values.add(current)
                    current += step
    
    if len(all_values) == max_val - min_val + 1:
        return '*'
    
    # Combine the results, preserving step notation
    result = regular_ranges + step_parts
    return ','.join(result)


def systemd_to_cron(systemd_timer):
    """
    Convert a systemd timer format to Ansible cron parameters
    
    Args:
        systemd_timer (str): The systemd timer format string
        
    Returns:
        dict: A dictionary with keys 'minute', 'hour', 'day', 'month', 'weekday'
              compatible with Ansible's cron module, plus 'warnings' and 'error' fields
    """
    """Main conversion function with enhanced validation"""
    result = {
        'minute': '',
        'hour': '',
        'day': '',
        'month': '',
        'weekday': '',
        'warnings': []
    }
    # result = {
    #     'minute': '*',
    #     'hour': '*',
    #     'day': '*',
    #     'month': '*',
    #     'weekday': '*',
    #     'warnings': []
    # }


    try:
        # Validate input type and presence
        if not isinstance(systemd_timer, str) or not systemd_timer.strip():
            raise AnsibleFilterError(
                "Invalid input type. Expected non-empty string, got: "
                f"{type(systemd_timer)}"
            )

        systemd_timer = systemd_timer.strip()

        # Handle special keywords first
        if systemd_timer.lower() in SPECIAL_KEYWORDS:
            result.update(SPECIAL_KEYWORDS[systemd_timer.lower()])
            return result

        # Parse systemd components
        calendar_parts = _parse_systemd_calendar(systemd_timer)
        
        # Validate weekday specification
        result['weekday'] = _parse_weekday(calendar_parts['weekday'])
        
        # Validate date components
        year, month, day, date_warnings = _parse_date_components(calendar_parts['date'])
        result['month'] = month
        result['day'] = day
        result['warnings'].extend(date_warnings)
        
        # Validate time components
        hour, minute, second, time_warnings = _parse_time_components(calendar_parts['time'])
        result['hour'] = hour
        result['minute'] = minute
        result['warnings'].extend(time_warnings)

          # Validate and simplify cron components
        for field, (min_val, max_val) in {
            'minute': (0, 59),
            'hour': (0, 23),
            'day': (1, 31),
            'month': (1, 12),
            'weekday': (0, 6)
        }.items():
            _validate_cron_component(result[field], field, min_val, max_val)
            result[field] = _simplify_crontab(result[field], field, min_val, max_val)
            _validate_cron_component(result[field], field, min_val, max_val)


        # Handle timezone warnings
        if calendar_parts['timezone']:
            result['warnings'].append(
                f"Ignored timezone specification: {calendar_parts['timezone']}"
            )

        return {k: v for k, v in result.items() if v is not None}

    except AnsibleFilterError:
        raise  # Re-raise properly annotated errors
    except Exception as e:
        raise AnsibleFilterError(
            f"Failed to convert systemd timer '{systemd_timer}': {to_native(e)}"
        ) from e


class FilterModule(object):
    """Ansible filter for converting systemd timer formats to cron parameters"""
    
    def filters(self):
        """Return a dictionary of filters provided by this module"""
        return {
            'systemd_to_cron': systemd_to_cron
        }
