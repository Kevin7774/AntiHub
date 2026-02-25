import React from "react";

type ErrorBoundaryProps = {
  children: React.ReactNode;
};

type ErrorBoundaryState = {
  hasError: boolean;
  traceId: string;
};

function nextTraceId(): string {
  return `ui-${Math.random().toString(36).slice(2, 10)}`;
}

export default class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public state: ErrorBoundaryState = {
    hasError: false,
    traceId: "",
  };

  public static getDerivedStateFromError(): Partial<ErrorBoundaryState> {
    return { hasError: true, traceId: nextTraceId() };
  }

  public componentDidCatch(): void {
    // Intentionally keep this silent in production UI.
  }

  private handleReload = () => {
    window.location.reload();
  };

  public render(): React.ReactNode {
    if (this.state.hasError) {
      return (
        <div className="ui-error-boundary" role="alert">
          <div className="ui-error-card">
            <div className="ui-error-title">页面出现异常</div>
            <div className="ui-error-desc">我们已拦截本次渲染错误，核心服务仍可用。请刷新页面继续操作。</div>
            <div className="ui-error-trace">
              Trace ID: <span className="mono">{this.state.traceId}</span>
            </div>
            <button className="primary" type="button" onClick={this.handleReload}>
              重新加载
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
