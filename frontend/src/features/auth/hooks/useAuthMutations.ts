import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  changePassword,
  login,
  logout,
  register,
  testModelSettings,
  updateModelSettings,
  updateNickname,
} from "@/shared/api/auth";
import { AUTH_ME_QUERY_KEY } from "@/features/auth/hooks/useAuthMe";
import { CHAT_SESSIONS_QUERY_KEY } from "@/features/chat/hooks/useChatSessions";

export function useAuthMutations() {
  const queryClient = useQueryClient();

  const loginMutation = useMutation({
    mutationFn: login,
    onSuccess: (user) => {
      queryClient.setQueryData(AUTH_ME_QUERY_KEY, user);
      void queryClient.invalidateQueries({ queryKey: AUTH_ME_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: CHAT_SESSIONS_QUERY_KEY });
    },
  });

  const registerMutation = useMutation({
    mutationFn: register,
    onSuccess: (user) => {
      queryClient.setQueryData(AUTH_ME_QUERY_KEY, user);
      void queryClient.invalidateQueries({ queryKey: AUTH_ME_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: CHAT_SESSIONS_QUERY_KEY });
    },
  });

  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: (user) => {
      queryClient.setQueryData(AUTH_ME_QUERY_KEY, user);
      void queryClient.invalidateQueries({ queryKey: AUTH_ME_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: CHAT_SESSIONS_QUERY_KEY });
    },
  });

  const nicknameMutation = useMutation({
    mutationFn: updateNickname,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: AUTH_ME_QUERY_KEY });
    },
  });

  const changePasswordMutation = useMutation({
    mutationFn: changePassword,
  });

  const modelSettingsMutation = useMutation({
    mutationFn: updateModelSettings,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: AUTH_ME_QUERY_KEY });
    },
  });

  const modelSettingsTestMutation = useMutation({
    mutationFn: testModelSettings,
  });

  return {
    loginMutation,
    registerMutation,
    logoutMutation,
    nicknameMutation,
    changePasswordMutation,
    modelSettingsMutation,
    modelSettingsTestMutation,
  };
}
