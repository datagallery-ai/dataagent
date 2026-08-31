export class ScrollAnchor {
  private anchorBottom = true;
  private userScrolled = false;
  private lastContentRows = 0;

  private shouldAnchorBottom(): boolean {
    return this.anchorBottom && !this.userScrolled;
  }

  handleUserScroll(scrollbackRows: number): void {
    const atBottom = scrollbackRows === 0;

    if (atBottom) {
      this.userScrolled = false;
      this.anchorBottom = true;
    } else {
      this.userScrolled = true;
      this.anchorBottom = false;
    }
  }

  handleContentGrowth(newContentRows: number, currentScrollback: number): number {
    const contentGrowth = newContentRows - this.lastContentRows;
    this.lastContentRows = newContentRows;

    if (this.shouldAnchorBottom()) {
      return 0;
    } else if (contentGrowth > 0) {
      return currentScrollback + contentGrowth;
    }

    return currentScrollback;
  }

  jumpToLatest(): void {
    this.userScrolled = false;
    this.anchorBottom = true;
  }

  reset(): void {
    this.anchorBottom = true;
    this.userScrolled = false;
    this.lastContentRows = 0;
  }
}
