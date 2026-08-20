// 历史会话页已并入观测体系（OBSERVABILITY_SPEC §3.1）：
// 会话详情在观测台 Trace 中体现。若存在旧入口，重定向到观测台。
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

export default function SessionsPage() {
  const navigate = useNavigate();
  useEffect(() => {
    navigate("/observability/traces", { replace: true });
  }, [navigate]);
  return null;
}
