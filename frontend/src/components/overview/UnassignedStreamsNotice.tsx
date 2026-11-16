import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link as RouterLink } from 'react-router-dom';
import { Alert, Button } from '@mui/material';
import { useUiText } from '../../i18n/useUiText';
import { acestreamChannelService, type UnassignedStreamsSummary } from '../../services/channelService';

/** Streams without a TV channel never reach a device playlist: an assignment rule is missing or failing. */
const UnassignedStreamsNotice: React.FC = () => {
  const tUi = useUiText();
  const { data } = useQuery<UnassignedStreamsSummary>({
    queryKey: ['unassigned-streams'],
    queryFn: acestreamChannelService.getUnassignedSummary,
    refetchInterval: 60_000,
  });
  if (!data || data.total === 0) return null;
  const recent = data.recent
    ? ` ${tUi("{{value0}} arrived in the last {{value1}} days, the latest: {{value2}}.", { value0: data.recent, value1: data.recent_days, value2: data.examples.join(', ') })}`
    : '';
  return (
    <Alert severity="warning" action={<Button color="inherit" component={RouterLink} to="/acestream-channels?assigned=false">{tUi("Review")}</Button>}>
      {`${tUi("{{value0}} sources have no TV channel: an assignment rule is missing or failing, and device playlists leave them out.", { value0: data.total })}${recent}`}
    </Alert>
  );
};

export default UnassignedStreamsNotice;
