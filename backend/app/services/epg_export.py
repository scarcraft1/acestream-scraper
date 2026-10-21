"""Playlist-aware XMLTV export and program windows. Shared session and service surface supplied by EPGService."""
import html
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from app.models.models import EPGChannel, EPGProgram, TVChannel


def overlay_matches(regular: List, matches: List) -> List:
    """Regular programmes with the matches laid over them, in start order.

    Where a match is on, it wins. A regular programme is cut around it, not
    dropped: a 3 h block with a 2 h match inside keeps its remaining hour.
    Pieces shorter than a minute are discarded. Programmes are copied, never
    modified, so nothing here can change what is stored.
    """
    if not matches:
        return list(regular)
    from types import SimpleNamespace
    spans = sorted((m.start_time, m.end_time) for m in matches)
    out = list(matches)
    for program in regular:
        pieces = [(program.start_time, program.end_time)]
        for m_start, m_end in spans:
            cut = []
            for p_start, p_end in pieces:
                if m_end <= p_start or m_start >= p_end:
                    cut.append((p_start, p_end))
                    continue
                if p_start < m_start:
                    cut.append((p_start, m_start))
                if m_end < p_end:
                    cut.append((m_end, p_end))
            pieces = cut
        for p_start, p_end in pieces:
            if (p_end - p_start) < timedelta(minutes=1):
                continue
            if (p_start, p_end) == (program.start_time, program.end_time):
                out.append(program)
            else:
                out.append(SimpleNamespace(
                    start_time=p_start, end_time=p_end, title=program.title,
                    subtitle=program.subtitle, description=program.description,
                    category=program.category,
                    image_url=getattr(program, 'image_url', None)))
    return sorted(out, key=lambda p: p.start_time)


class EPGExportOperations:
    db: Session

    def generate_epg_xml(self, search_term: Optional[str] = None, favorites_only: bool = False,
                         days_back: int = 1, days_forward: int = 7,
                         tv_channel_ids: Optional[List[int]] = None) -> str:
        """Generate the XMLTV guide for TV channels that have EPG data.

        Args:
            search_term: Optional search term to filter channels by name
            favorites_only: If True, only include favorite channels
            days_back: Number of days in the past to include programs for
            days_forward: Number of days in the future to include programs for
            tv_channel_ids: Restrict the export to these TV channels (the tuner
                lineup uses it); None keeps the historical output unchanged.

        Returns:
            String containing the XML EPG content in XMLTV format
        """
        xml_lines = [
            '<?xml version="1.0" encoding="utf-8" ?>',
            '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
            '<tv generator-info-name="Acestream Scraper EPG Generator" generator-info-url="https://github.com/pipepito/acestream-scraper">'
        ]

        # A channel with only a football agenda (no regular guide) is
        # exported too: «M+ Liga de Campeones 7» may have nothing else.
        tv_channels_query = self.db.query(TVChannel).filter(
            (TVChannel.epg_id.isnot(None)) | (TVChannel.agenda_epg_id.isnot(None)))

        if search_term:
            tv_channels_query = tv_channels_query.filter(TVChannel.name.ilike(f"%{search_term}%"))

        if favorites_only:
            tv_channels_query = tv_channels_query.filter(TVChannel.is_favorite == True)

        if tv_channel_ids is not None:
            tv_channels_query = tv_channels_query.filter(TVChannel.id.in_(list(tv_channel_ids)))

        sorted_channels = sorted(
            tv_channels_query.all(),
            key=lambda c: (c.channel_number is None, c.channel_number or 0, c.name.lower())
        )
        epg_lookup = self.epg_channel_lookup(sorted_channels)

        from app.services.epg_identity_service import EPGIdentityService
        identities = EPGIdentityService(self.db)
        channel_epg_mappings = []
        emitted = set()

        agenda_lookup = self.agenda_channel_lookup(sorted_channels)
        for tv_channel in sorted_channels:
            epg_channel = (epg_lookup.get((tv_channel.epg_source_id, tv_channel.epg_id))
                           if tv_channel.epg_id else None)
            agenda_channel = agenda_lookup.get(tv_channel.agenda_epg_id) if tv_channel.agenda_epg_id else None
            if epg_channel is None and agenda_channel is None:
                continue

            # The id Kodi matches against the playlist's tvg-id. A regular
            # guide keeps its resolved id; an agenda-only channel uses the
            # agenda id, which the playlist emits for it too.
            epg_id = (identities.resolve(tv_channel.epg_source_id, tv_channel.epg_id)
                      if epg_channel is not None else tv_channel.agenda_epg_id)
            if epg_id in emitted:
                continue
            emitted.add(epg_id)
            display_name = tv_channel.name

            channel_epg_mappings.append({
                'epg_id': epg_id,
                'display_name': display_name,
                'tv_channel': tv_channel,
                'epg_channel': epg_channel,
                'agenda_channel': agenda_channel,
            })

        # Generate channel definitions
        for mapping in channel_epg_mappings:
            tv_channel = mapping['tv_channel']

            xml_lines.append(f'  <channel id="{html.escape(mapping["epg_id"])}">')
            xml_lines.append(f'    <display-name>{html.escape(mapping["display_name"])}</display-name>')

            if tv_channel.logo_url:
                xml_lines.append(f'    <icon src="{html.escape(tv_channel.logo_url)}" />')

            xml_lines.append('  </channel>')

        xml_lines.append('')

        programs_by_channel = self.programs_in_window(
            [m[k].id for m in channel_epg_mappings
             for k in ('epg_channel', 'agenda_channel') if m[k] is not None],
            days_back,
            days_forward,
        )
        for mapping in channel_epg_mappings:
            regular = (programs_by_channel.get(mapping['epg_channel'].id, [])
                       if mapping['epg_channel'] is not None else [])
            matches = (programs_by_channel.get(mapping['agenda_channel'].id, [])
                       if mapping['agenda_channel'] is not None else [])
            for program in overlay_matches(regular, matches):
                xml_lines.extend(self.programme_xml_lines(program, mapping['epg_id']))

        xml_lines.append('</tv>')

        return '\n'.join(xml_lines)

    def agenda_channel_lookup(self, tv_channels: List[TVChannel]) -> Dict[str, EPGChannel]:
        """Map agenda_epg_id to its guide channel, in any ENABLED source.

        Agenda ids carry their own prefix (`futbolenlatv.`), so they are
        unique without a source id; a disabled agenda source exports nothing.
        """
        ids = {c.agenda_epg_id for c in tv_channels if c.agenda_epg_id}
        if not ids:
            return {}
        from app.models.models import EPGSource
        rows = (
            self.db.query(EPGChannel).join(EPGSource)
            .filter(EPGChannel.channel_xml_id.in_(ids), EPGSource.enabled.is_(True))
            .order_by(EPGChannel.id)
            .all()
        )
        lookup: Dict[str, EPGChannel] = {}
        for row in rows:
            lookup.setdefault(row.channel_xml_id, row)
        return lookup

    def epg_channel_lookup(self, tv_channels: List[TVChannel]) -> Dict[Tuple[Optional[int], str], EPGChannel]:
        """Map (epg_source_id, channel_xml_id) to the EPG channel each TV channel points at."""
        source_ids = {channel.epg_source_id for channel in tv_channels if channel.epg_source_id is not None}
        xml_ids = {channel.epg_id for channel in tv_channels if channel.epg_id}
        if not source_ids or not xml_ids:
            return {}
        epg_channels = (
            self.db.query(EPGChannel)
            .filter(
                EPGChannel.epg_source_id.in_(source_ids),
                EPGChannel.channel_xml_id.in_(xml_ids),
            )
            .all()
        )
        return {
            (epg_channel.epg_source_id, epg_channel.channel_xml_id): epg_channel
            for epg_channel in epg_channels
        }

    def programs_in_window(self, epg_channel_ids: List[int], days_back: int = 1,
                           days_forward: int = 7) -> Dict[int, List[EPGProgram]]:
        """Programs inside the export window, grouped by EPG channel id and ordered by start time."""
        if not epg_channel_ids:
            return {}
        now = datetime.now(timezone.utc)
        programs = (
            self.db.query(EPGProgram)
            .filter(
                EPGProgram.epg_channel_id.in_(epg_channel_ids),
                EPGProgram.start_time >= now - timedelta(days=days_back),
                EPGProgram.end_time <= now + timedelta(days=days_forward),
            )
            .order_by(EPGProgram.epg_channel_id, EPGProgram.start_time)
            .all()
        )
        programs_by_channel: Dict[int, List[EPGProgram]] = {}
        for program in programs:
            programs_by_channel.setdefault(program.epg_channel_id, []).append(program)
        return programs_by_channel

    @staticmethod
    def programme_xml_lines(program: EPGProgram, channel_id: str) -> List[str]:
        """The XMLTV <programme> block for one program, filed under channel_id."""
        start_time_str = program.start_time.strftime("%Y%m%d%H%M%S %z")
        stop_time_str = program.end_time.strftime("%Y%m%d%H%M%S %z")

        if '+' not in start_time_str and '-' not in start_time_str:
            start_time_str += ' +0000'
        if '+' not in stop_time_str and '-' not in stop_time_str:
            stop_time_str += ' +0000'

        lines = [
            f'  <programme start="{start_time_str}" stop="{stop_time_str}" channel="{html.escape(channel_id)}">',
            f'    <title>{html.escape(program.title)}</title>',
        ]

        if program.subtitle:
            lines.append(f'    <sub-title>{html.escape(program.subtitle)}</sub-title>')

        if program.description:
            lines.append(f'    <desc>{html.escape(program.description)}</desc>')

        if program.category:
            lines.append(f'    <category>{html.escape(program.category)}</category>')

        if program.image_url:
            lines.append(f'    <icon src="{html.escape(program.image_url)}" />')

        lines.append('  </programme>')
        return lines
