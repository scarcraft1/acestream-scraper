import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from '@tanstack/react-query';
import {
  devicePlaylistService,
  type ChannelGroup,
  type DevicePlaylist,
  type DevicePlaylistCreate,
  type DevicePlaylistUpdate,
} from '../services/devicePlaylistService';

const KEY = ['device-playlists'];

export const useDevicePlaylists = (options?: Omit<UseQueryOptions<DevicePlaylist[]>, 'queryKey' | 'queryFn'>) =>
  useQuery<DevicePlaylist[]>({ queryKey: KEY, queryFn: devicePlaylistService.list, ...options });

export const useDevicePlaylistGroups = () =>
  useQuery<ChannelGroup[]>({ queryKey: [...KEY, 'groups'], queryFn: devicePlaylistService.groups });

export const useCreateDevicePlaylist = () => {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DevicePlaylistCreate) => devicePlaylistService.create(body),
    onSuccess: () => client.invalidateQueries({ queryKey: KEY }),
  });
};

export const useUpdateDevicePlaylist = () => {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: DevicePlaylistUpdate }) => devicePlaylistService.update(id, body),
    onSuccess: (saved) => client.setQueryData<DevicePlaylist[]>(KEY, (rows) => rows?.map((row) => (row.id === saved.id ? saved : row))),
  });
};

export const useDeleteDevicePlaylist = () => {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => devicePlaylistService.remove(id),
    onSuccess: () => client.invalidateQueries({ queryKey: KEY }),
  });
};
