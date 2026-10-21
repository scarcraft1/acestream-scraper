import { act, renderHook } from '@testing-library/react';
import React from 'react';
import { useTVChannelForm } from '../hooks/useTVChannelForm';
import { TVChannel } from '../types/tvChannelTypes';

const channel = {
  id: 3,
  name: 'M+ LaLiga',
  is_active: true,
  is_favorite: false,
  epg_id: 'MLaLiga.es',
  epg_source_id: 4,
  agenda_epg_id: 'futbolenlatv.m-plus-laliga',
  channel_number: 7,
  created_at: '2026-10-14T00:00:00Z',
  updated_at: '2026-10-14T00:00:00Z',
} as unknown as TVChannel;

const change = (name: string, value: string) =>
  ({ target: { name, value, type: 'text', checked: false } }) as unknown as React.ChangeEvent<HTMLInputElement>;

describe('football agenda field', () => {
  it('loads the current agenda into the edit form', () => {
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    expect(result.current.formData.agenda_epg_id).toBe('futbolenlatv.m-plus-laliga');
  });

  it('sends null when the field is emptied, so the agenda is removed', () => {
    // An emptied field that is silently ignored cannot be undone from the
    // form: that is what happens to the channel number.
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    act(() => result.current.handleFormChange(change('agenda_epg_id', '  ')));
    let payload: ReturnType<typeof result.current.validateForm> = null;
    act(() => {
      payload = result.current.validateForm();
    });
    expect(payload).not.toBeNull();
    expect(payload).toHaveProperty('agenda_epg_id', null);
  });

  it('sends the trimmed id when it is set', () => {
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    act(() => result.current.handleFormChange(change('agenda_epg_id', ' futbolenlatv.dazn-laliga ')));
    let payload: ReturnType<typeof result.current.validateForm> = null;
    act(() => {
      payload = result.current.validateForm();
    });
    expect(payload).toHaveProperty('agenda_epg_id', 'futbolenlatv.dazn-laliga');
  });
});

describe('channel number in the edit form', () => {
  const submit = (value: string) => {
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    act(() => result.current.handleFormChange({ target: { name: 'channel_number', value, type: 'number', checked: false } } as unknown as React.ChangeEvent<HTMLInputElement>));
    let payload: ReturnType<typeof result.current.validateForm> = null;
    act(() => {
      payload = result.current.validateForm();
    });
    return payload;
  };

  it('sends null when the number is emptied, so it is removed', () => {
    expect(submit('')).toHaveProperty('channel_number', null);
  });

  it('keeps 0 as a number', () => {
    expect(submit('0')).toHaveProperty('channel_number', 0);
  });
});

describe('guide source in the edit form', () => {
  const submit = (value: string | number | null) => {
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    if (value !== null) {
      act(() => result.current.handleFormChange({ target: { name: 'epg_source_id', value, type: 'text', checked: false } } as unknown as React.ChangeEvent<HTMLInputElement>));
    }
    let payload: ReturnType<typeof result.current.validateForm> = null;
    act(() => {
      payload = result.current.validateForm();
    });
    return payload;
  };

  it('keeps the loaded source', () => {
    expect(submit(null)).toHaveProperty('epg_source_id', 4);
  });

  it('sends the chosen source as a number', () => {
    expect(submit(9)).toHaveProperty('epg_source_id', 9);
  });

  it('sends null when set to Not set', () => {
    expect(submit('')).toHaveProperty('epg_source_id', null);
  });
});
