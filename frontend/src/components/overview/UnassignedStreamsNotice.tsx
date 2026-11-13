import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link as RouterLink } from 'react-router-dom';
import { Alert, Button } from '@mui/material';
import { acestreamChannelService, type UnassignedStreamsSummary } from '../../services/channelService';

/** Streams without a TV channel never reach a device playlist: an assignment rule is missing or failing. */
const UnassignedStreamsNotice: React.FC = () => {
  const { data } = useQuery<UnassignedStreamsSummary>({
    queryKey: ['unassigned-streams'],
    queryFn: acestreamChannelService.getUnassignedSummary,
    refetchInterval: 60_000,
  });
  if (!data || data.total === 0) return null;
  const recent = data.recent
    ? ` ${data.recent} arrived in the last ${data.recent_days} days, the latest: ${data.examples.join(', ')}.`
    : '';
  return (
    <Alert severity="warning" action={<Button color="inherit" component={RouterLink} to="/acestream-channels?assigned=false">Review</Button>}>
      {`${data.total} sources have no TV channel: an assignment rule is missing or failing, and device playlists leave them out.${recent}`}
    </Alert>
  );
};

export default UnassignedStreamsNotice;
