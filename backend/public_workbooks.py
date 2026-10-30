"""Small, bounded XLSX reader for public statistical tables (no Excel runtime)."""
import io
from decimal import Decimal
import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from .bop import BOPError

MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def read_xlsx_sheet(body, sheet_name, formatted_header_rows=0, max_expanded_bytes=40_000_000):
    if len(body) > 8_000_000:
        raise BOPError('Statistical workbook exceeds the download limit.')
    try:
        with ZipFile(io.BytesIO(body)) as archive:
            files = archive.infolist()
            if not 0 < max_expanded_bytes <= 60_000_000 or len(files) > 1000 or len({f.filename for f in files}) != len(files) or sum(f.file_size for f in files) > max_expanded_bytes:
                raise BOPError('Statistical workbook has an invalid or oversized ZIP structure.')

            def xml(path):
                data = archive.read(path)
                if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                    raise BOPError('Unexpected document declarations in a statistical workbook.')
                return ET.fromstring(data)

            workbook = xml('xl/workbook.xml')
            candidates = [sheet for sheet in workbook.findall(MAIN + 'sheets/' + MAIN + 'sheet')
                          if sheet.attrib['name'].strip().casefold() == sheet_name.strip().casefold()]
            if len(candidates) != 1:
                raise BOPError('The expected statistical workbook sheet is missing or duplicated.')
            relation_id = candidates[0].attrib[REL + 'id']
            relationships = xml('xl/_rels/workbook.xml.rels')
            links = [link for link in relationships if link.attrib.get('Id') == relation_id and link.attrib.get('TargetMode') != 'External']
            if len(links) != 1:
                raise BOPError('The statistical workbook has an invalid sheet relationship.')
            target = links[0].attrib['Target']
            path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
            if not path.startswith('xl/'):
                raise BOPError('The statistical workbook references a sheet outside its data directory.')
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(item.itertext()) for item in xml('xl/sharedStrings.xml')]
            formats, cell_formats = {1:'0',2:'0.00'}, []
            if formatted_header_rows:
                styles = xml('xl/styles.xml')
                formats.update({int(item.attrib['numFmtId']):item.attrib['formatCode'] for item in styles.findall(MAIN+'numFmts/'+MAIN+'numFmt')})
                cell_formats = [int(item.attrib.get('numFmtId',0)) for item in styles.findall(MAIN+'cellXfs/'+MAIN+'xf')]
            rows = {}
            cells_seen = 0
            for row in xml(path).findall(MAIN + 'sheetData/' + MAIN + 'row'):
                index = int(row.attrib['r']) - 1
                if index < 0 or index >= 5000 or index in rows:
                    raise BOPError('Invalid or duplicate spreadsheet rows.')
                values = {}
                for cell in row.findall(MAIN + 'c'):
                    cells_seen += 1
                    match = re.fullmatch(r'([A-Z]+)(\d+)', cell.attrib['r'])
                    if not match or int(match[2]) != index + 1:
                        raise BOPError('Invalid spreadsheet cell coordinates.')
                    column = 0
                    for letter in match[1]:
                        column = column * 26 + ord(letter) - 64
                    column -= 1
                    if column > 255 or column in values or cells_seen > 250_000:
                        raise BOPError('Invalid or oversized spreadsheet cells.')
                    value = cell.findtext(MAIN + 'v')
                    kind = cell.attrib.get('t')
                    if kind == 's':
                        value = strings[int(value)]
                    elif kind == 'inlineStr':
                        inline = cell.find(MAIN + 'is')
                        value = ''.join(inline.itertext()) if inline is not None else None
                    elif kind == 'e' and value:
                        raise BOPError('An official spreadsheet contains an unresolved cell error.')
                    elif value is not None and index < formatted_header_rows and 's' in cell.attrib:
                        # SAFE uses 2015.1 with Excel's 0.00 format to mean
                        # October (2015.10). Apply formatting to headers only;
                        # data cells always retain their full native precision.
                        code = formats.get(cell_formats[int(cell.attrib['s'])],'')
                        fixed = re.fullmatch(r'0\.(0+)(?:_.*)?',code.split(';')[0])
                        if fixed:
                            value = format(Decimal(value),f'.{len(fixed[1])}f')
                    values[column] = value
                rows[index] = values
            if not rows:
                raise BOPError('The official spreadsheet sheet is empty.')
            return [[rows.get(index, {}).get(column) for column in range(max(rows.get(index, {}), default=0) + 1)]
                    for index in range(max(rows) + 1)]
    except (BadZipFile, ET.ParseError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise BOPError('The official source did not return a valid XLSX statistical table.') from exc
