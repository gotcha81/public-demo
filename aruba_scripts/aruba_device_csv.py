"""
Read and check the Aruba device CSV. No API calls here, only the file.

Mandatory columns: serial number, mac address, subscription code, msp portal, tenant
Optional columns:  site, group, template (an empty cell gets the default value)

Header names are forgiving: "Serial Number", "serial_number" and "serial" all work.
Both "," and ";" work as separators (Excel in Sweden often saves with ";").
"""

import csv
import re

# Header in the file (lower case, spaces -> "_")  ->  name used in the script
COLUMN_NAMES = {
    "serial_number": "serial", "serial": "serial", "serial_no": "serial",
    "mac_address": "mac", "mac": "mac",
    "subscription_code": "subscription", "subscription_key": "subscription", "subscription": "subscription",
    "msp_portal": "msp_portal", "msp": "msp_portal", "portal": "msp_portal",
    "tenant": "tenant", "customer": "tenant",
    "site": "site",
    "group": "group",
    "template": "template",
}
REQUIRED = ["serial", "mac", "subscription", "msp_portal", "tenant"]
OPTIONAL = ["site", "group", "template"]


def normalise_header(header):
    """'Serial Number ' -> 'serial_number'"""
    return re.sub(r"[\s\-.]+", "_", header.strip().lower())


def normalise_mac(mac):
    """'aa-bb-cc-dd-ee-ff', 'aabb.ccdd.eeff', ... -> 'AA:BB:CC:DD:EE:FF'. Returns None if it isn't a MAC."""
    hex_digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hex_digits) != 12 or re.search(r"[^0-9a-fA-F:\-.\s]", mac):
        return None
    hex_digits = hex_digits.upper()
    return ":".join(hex_digits[i:i + 2] for i in range(0, 12, 2))


def open_csv(path):
    """Open the file and return a csv.reader, using "," or ";" depending on the file."""
    f = open(path, newline="", encoding="utf-8-sig")
    first_line = f.readline()
    f.seek(0)
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    return f, csv.reader(f, delimiter=delimiter)


def read_header(reader):
    """Read the header row and return {column_index: our_name}. Raises ValueError if columns are missing."""
    header = next(reader, None)
    if not header:
        raise ValueError("the file is empty")

    columns = {}
    unknown = []
    for index, raw_name in enumerate(header):
        our_name = COLUMN_NAMES.get(normalise_header(raw_name))
        if our_name:
            columns[index] = our_name
        elif raw_name.strip():
            unknown.append(raw_name)

    missing = [name for name in REQUIRED if name not in columns.values()]
    if missing:
        raise ValueError(f"missing mandatory column(s) {missing}. Header is {header}")
    return columns, unknown


def check_row(row):
    """Check the values in one row. Returns a list of problems (empty list = OK). Also cleans up the MAC."""
    problems = []
    for name in REQUIRED:
        if not row[name]:
            problems.append(f"'{name}' is empty (mandatory)")

    if row["serial"] and not re.fullmatch(r"[A-Za-z0-9]+", row["serial"]):
        problems.append(f"serial '{row['serial']}' should only contain letters and digits")

    if row["mac"]:
        clean_mac = normalise_mac(row["mac"])
        if clean_mac:
            row["mac"] = clean_mac
        else:
            problems.append(f"mac '{row['mac']}' is not a valid MAC address")

    if row["subscription"] and not re.fullmatch(r"[A-Za-z0-9]+", row["subscription"]):
        problems.append(f"subscription code '{row['subscription']}' should only contain letters and digits")
    return problems


def read_device_rows(path, defaults, log):
    """Read the CSV. Returns (good_rows, bad_rows).

    good_rows: list of dicts with serial, mac, subscription, msp_portal, tenant, site, group, template,
               line (line number in the file) and defaults_used (which optional values were empty).
    bad_rows:  list of (line, serial, reason) for rows that can't be used.
    Raises OSError / ValueError if the file itself can't be read.
    """
    f, reader = open_csv(path)
    with f:
        columns, unknown = read_header(reader)
        if unknown:
            log.warning("Ignoring unknown column(s) in the CSV: %s", unknown)

        good_rows, bad_rows = [], []
        seen_serials, seen_macs = {}, {}
        for values in reader:
            line = reader.line_num
            if not any(v.strip() for v in values):
                continue  # empty line

            row = {name: "" for name in REQUIRED + OPTIONAL}
            for index, our_name in columns.items():
                row[our_name] = values[index].strip() if index < len(values) else ""
            row["serial"] = row["serial"].upper()
            row["line"] = line

            # Empty optional values get the defaults (placeholder site, group and template)
            row["defaults_used"] = []
            for name in OPTIONAL:
                if not row[name] and defaults.get(name):
                    row[name] = defaults[name]
                    row["defaults_used"].append(name)

            problems = check_row(row)
            if row["serial"] in seen_serials:
                problems.append(f"serial is also on line {seen_serials[row['serial']]}")
            if row["mac"] and row["mac"] in seen_macs:
                problems.append(f"mac is also on line {seen_macs[row['mac']]}")
            if problems:
                bad_rows.append((line, row["serial"] or "?", "; ".join(problems)))
                continue

            seen_serials[row["serial"]] = line
            seen_macs[row["mac"]] = line
            if len(row["subscription"]) != 14:
                log.warning("CSV line %d: subscription code '%s' is %d characters, Aruba codes are usually 14",
                            line, row["subscription"], len(row["subscription"]))
            good_rows.append(row)
    return good_rows, bad_rows
