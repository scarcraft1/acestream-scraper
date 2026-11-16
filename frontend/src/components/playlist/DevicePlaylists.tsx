import React, { useMemo, useState } from 'react';
import {
  Alert, Autocomplete, Box, Button, Checkbox, Divider, FormControlLabel, Grid, List, ListItemButton, ListItemText, Stack,
  TextField, Typography,
} from '@mui/material';
import { useUiText } from '../../i18n/useUiText';
import { useConfirm } from '../ConfirmDialog';
import {
  useCreateDevicePlaylist, useDeleteDevicePlaylist, useDevicePlaylistGroups, useDevicePlaylists, useUpdateDevicePlaylist,
} from '../../hooks/useDevicePlaylists';
import { useTVChannelCatalog } from '../../hooks/useTVChannels';
import type { ChannelGroup, DevicePlaylist, DevicePlaylistUpdate } from '../../services/devicePlaylistService';

interface ChannelOption { id: number; name: string }

/** Groups in tab order, each followed by its subgroups, with their depth. */
const groupTree = (groups: ChannelGroup[]): { group: ChannelGroup; depth: number }[] => {
  const kids = new Map<number | null, ChannelGroup[]>();
  for (const group of groups) {
    const parent = group.parent_id ?? null;
    kids.set(parent, [...(kids.get(parent) ?? []), group]);
  }
  const out: { group: ChannelGroup; depth: number }[] = [];
  const seen = new Set<number>();
  const walk = (parent: number | null, depth: number) => {
    for (const group of [...(kids.get(parent) ?? [])].sort((a, b) => a.position - b.position)) {
      if (seen.has(group.id)) continue;
      seen.add(group.id);
      out.push({ group, depth });
      walk(group.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
};

/** One M3U per device: which groups and channels each one leaves out. */
const DevicePlaylists: React.FC = () => {
  const tUi = useUiText();
  const { data: playlists = [], isLoading, isError } = useDevicePlaylists();
  const { data: groups = [] } = useDevicePlaylistGroups();
  const { data: channels = [] } = useTVChannelCatalog();
  const create = useCreateDevicePlaylist();
  const update = useUpdateDevicePlaylist();
  const remove = useDeleteDevicePlaylist();
  const { confirm, dialog } = useConfirm();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [newName, setNewName] = useState('');
  const [channel, setChannel] = useState<ChannelOption | null>(null);

  const selected: DevicePlaylist | undefined = playlists.find((row) => row.id === selectedId) ?? playlists[0];
  const tree = useMemo(() => groupTree(groups), [groups]);
  const byId = useMemo(() => new Map(groups.map((group) => [group.id, group])), [groups]);
  const channelNames = useMemo(() => new Map(channels.map((tv) => [tv.id, tv.name])), [channels]);
  const options: ChannelOption[] = useMemo(() => channels.map((tv) => ({ id: tv.id, name: tv.name })), [channels]);

  const save = (body: DevicePlaylistUpdate) => { if (selected) update.mutate({ id: selected.id, body }); };
  const disabledGroups = selected?.disabled_groups ?? [];
  const off = new Set(disabledGroups);
  const offAncestor = (group: ChannelGroup): ChannelGroup | undefined => {
    let parent = group.parent_id != null ? byId.get(group.parent_id) : undefined;
    const seen = new Set<number>();
    while (parent && !seen.has(parent.id)) {
      if (off.has(parent.id)) return parent;
      seen.add(parent.id);
      parent = parent.parent_id != null ? byId.get(parent.parent_id) : undefined;
    }
    return undefined;
  };
  const toggleGroup = (id: number, enabled: boolean) =>
    save({ disabled_groups: enabled ? disabledGroups.filter((value) => value !== id) : [...disabledGroups, id] });
  const setChannelState = (id: number, state: 'on' | 'off' | 'groups') => {
    if (!selected) return;
    const enabled = selected.enabled_channels.filter((value) => value !== id);
    const disabled = selected.disabled_channels.filter((value) => value !== id);
    save({
      enabled_channels: state === 'on' ? [...enabled, id] : enabled,
      disabled_channels: state === 'off' ? [...disabled, id] : disabled,
    });
  };
  const add = async () => {
    const name = newName.trim();
    if (!name) return;
    const row = await create.mutateAsync({ name });
    setNewName('');
    setSelectedId(row.id);
  };
  const removeSelected = async () => {
    if (!selected) return;
    if (await confirm({ title: tUi("Delete «{{value0}}»?", { value0: selected.name }), body: tUi("Players using its link get the whole catalogue back."), confirmLabel: tUi("Delete"), danger: true })) {
      remove.mutate(selected.id);
      setSelectedId(null);
    }
  };
  const overrides = selected ? [
    ...selected.enabled_channels.map((id) => ({ id, enabled: true })),
    ...selected.disabled_channels.map((id) => ({ id, enabled: false })),
  ] : [];

  return (
    <Grid container spacing={3}>
      {dialog}
      <Grid item xs={12} md={4}>
        <Stack spacing={1.5}>
          <Typography variant="sectionTitle" component="h3">{tUi("Devices")}</Typography>
          {isError ? <Alert severity="warning">{tUi("Could not load device playlists.")}</Alert> : null}
          {!isLoading && !playlists.length ? <Typography variant="body2" color="text.secondary">{tUi("No device playlists yet. The playlist has every channel.")}</Typography> : null}
          <List dense disablePadding aria-label={tUi("Device playlists")}>
            {playlists.map((row) => (
              <ListItemButton key={row.id} selected={row.id === selected?.id} onClick={() => setSelectedId(row.id)}>
                <ListItemText primary={row.name} secondary={tUi("{{value0}} of {{value1}} channels", { value0: row.channels, value1: row.total })} />
              </ListItemButton>
            ))}
          </List>
          <Stack direction="row" spacing={1} component="form" onSubmit={(event) => { event.preventDefault(); void add(); }}>
            <TextField size="small" fullWidth label={tUi("New device")} placeholder={tUi("Kitchen TV")} value={newName} onChange={(event) => setNewName(event.target.value)} inputProps={{ maxLength: 120 }} />
            <Button type="submit" variant="outlined" disabled={!newName.trim() || create.isPending}>{tUi("Add")}</Button>
          </Stack>
        </Stack>
      </Grid>
      <Grid item xs={12} md={8}>
        {selected ? (
          <Stack spacing={2}>
            <Box>
              <Typography variant="sectionTitle" component="h3">{selected.name}</Typography>
              <Typography variant="body2" color="text.secondary" role="status">
                {tUi("{{value0}} of {{value1}} channels · {{value2}} tabs", { value0: selected.channels, value1: selected.total, value2: selected.tabs })}
              </Typography>
            </Box>
            {update.isError ? <Alert severity="error">{tUi("Could not save the change. Try again.")}</Alert> : null}
            <Box>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                <Typography variant="subtitle2" component="h4" sx={{ flexGrow: 1 }}>{tUi("Groups")}</Typography>
                <Button size="small" onClick={() => save({ disabled_groups: [] })}>{tUi("Enable all")}</Button>
                <Button size="small" onClick={() => save({ disabled_groups: groups.filter((group) => group.parent_id == null).map((group) => group.id) })}>{tUi("Disable all")}</Button>
              </Stack>
              <Typography variant="body2" color="text.secondary">{tUi("Unticking a group disables it and its subgroups. A channel with any disabled group is left out.")}</Typography>
              <Stack>
                {tree.map(({ group, depth }) => {
                  const parent = offAncestor(group);
                  return (
                    <FormControlLabel
                      key={group.id}
                      sx={{ pl: depth * 3 }}
                      control={<Checkbox size="small" checked={!off.has(group.id) && !parent} disabled={Boolean(parent) || update.isPending}
                        onChange={(event) => toggleGroup(group.id, event.target.checked)} />}
                      label={parent ? tUi("{{value0}} — off because «{{value1}}» is", { value0: group.name, value1: parent.name }) : `${group.name} (${group.channels})`}
                    />
                  );
                })}
              </Stack>
            </Box>
            <Divider />
            <Box>
              <Typography variant="subtitle2" component="h4">{tUi("Single channels")}</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{tUi("Override the groups for one channel.")}</Typography>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <Autocomplete<ChannelOption>
                  size="small"
                  sx={{ flexGrow: 1 }}
                  options={options}
                  value={channel}
                  onChange={(_, value) => setChannel(value)}
                  getOptionLabel={(option) => option.name}
                  isOptionEqualToValue={(a, b) => a.id === b.id}
                  renderInput={(params) => <TextField {...params} label={tUi("TV channel")} />}
                />
                <Button variant="outlined" disabled={!channel} onClick={() => { if (channel) { setChannelState(channel.id, 'on'); setChannel(null); } }}>{tUi("Enable")}</Button>
                <Button variant="outlined" disabled={!channel} onClick={() => { if (channel) { setChannelState(channel.id, 'off'); setChannel(null); } }}>{tUi("Disable")}</Button>
              </Stack>
              <List dense aria-label={tUi("Channels set on their own")}>
                {overrides.map(({ id, enabled }) => (
                  <Stack key={id} direction="row" alignItems="center" spacing={1} sx={{ py: 0.25 }}>
                    <Typography variant="body2" sx={{ flexGrow: 1 }}>{channelNames.get(id) ?? tUi("Channel {{value0}}", { value0: id })}</Typography>
                    <Typography variant="body2" color={enabled ? 'success.main' : 'text.secondary'}>{enabled ? tUi("Enabled") : tUi("Disabled")}</Typography>
                    <Button size="small" onClick={() => setChannelState(id, 'groups')}>{tUi("Follow groups")}</Button>
                  </Stack>
                ))}
              </List>
            </Box>
            <Box>
              <Button color="error" onClick={() => void removeSelected()}>{tUi("Delete playlist")}</Button>
            </Box>
          </Stack>
        ) : null}
      </Grid>
    </Grid>
  );
};

export default DevicePlaylists;
